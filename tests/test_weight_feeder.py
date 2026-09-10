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
import io
import json
import math
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import loopback_server, plain_200, redirect_to

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_feeder.py"

_spec = importlib.util.spec_from_file_location("weight_feeder", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["weight_feeder"] = feeder
_spec.loader.exec_module(feeder)

NOW = 1789000000          # a fixed "now" so nothing here depends on the clock.
GOAL = 180.0              # for the injected loaders, which do not touch disk.
DEFS_GOAL = 170.0         # the target the Owner's real synced file carries.


def write_defs(tmp_path, files):
    """Materialise a definitions directory in the shape NagLight loads."""
    defs_dir = tmp_path / "definitions"
    defs_dir.mkdir(exist_ok=True)
    for name, body in files.items():
        (defs_dir / name).write_text(body, encoding="utf-8")
    return str(defs_dir)


# THE OWNER'S ACTUAL SYNCED FILE, not a fixture shaped to suit the parser. The
# Health category with `color_weight: 1.5`, the weigh-in item carrying
# `target: 170` / `unit: lb` / `horizon: long` / `recur: weekly`, and a sibling
# item either side of it - including one with a `target` of its own, because
# finding the goal by shape rather than by id would post a step count as a body
# weight. The prose body carries a `target:` that must NOT be read.
HEALTH_MD = """---
category: Health
color_weight: 1.5
items:
  - id: take-vitamins
    title: Take the vitamins
    type: habit
    recur: daily
    horizon: short
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: weekly
    horizon: long
    target: 170
    unit: lb
  - id: walk-steps
    title: Walk the daily steps
    type: metric
    recur: daily
    horizon: short
    target: 8000
    unit: steps
---

Free notes after the frontmatter. Someone might well write target: 999 down
here while thinking out loud, and it must not become the goal.
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


def test_a_future_stamp_is_refused_and_would_be_stale_anyway_sr022():
    """A future stamp is now REFUSED, not merely rendered stale.

    Two lines, and both are asserted because they fail differently. `is_fresh`
    was always the reader-side rule: a stamp ahead of the clock is stale. But
    the producer side used to build that body happily, and a future stamp is
    the one that fabricates freshness from the other direction — it keeps a
    dead source green until the stamp itself expires. `check_observed_at`
    refuses it rather than clamping it to `now`, because clamping would invent
    exactly the thing the invariant forbids: a stamp this cycle did not
    measure.
    """
    with pytest.raises(feeder.SourceFailure):
        feeder.build_post((191.4, NOW + 86400), None, GOAL, NOW)
    assert feeder.is_fresh({"observed_at": feeder.iso8601_utc(NOW + 86400)}, NOW) is False


def test_a_reading_this_cycle_took_must_still_be_a_plausible_weight_sr022():
    """`gauge_body` only checked finiteness, so `-500` and `100000` were bodies.

    The sentinel 0 is the ONE unmeasured number this file may emit and it is
    only ever paired with a missing stamp; every stamped body must carry a
    weight a person could have.
    """
    for absurd in (-500.0, 0.0, 100000.0, 0.001):
        with pytest.raises((ValueError, feeder.SourceFailure)):
            feeder.build_post((absurd, NOW), None, GOAL, NOW)
    sentinel, fresh = feeder.build_post(None, None, GOAL, NOW)
    assert sentinel["value"] == 0.0 and "observed_at" not in sentinel and not fresh


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
        goal_loader=lambda d, *a: (GOAL, "health.md"))
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
                     goal_loader=lambda d, *a: (GOAL, "health.md"))
    assert state_path.read_bytes() == before


# ═══════════════════════════════════════════════════════════════════════════
# B. "the goal lives in the user's definitions so it syncs like everything else"
#
# THE GOAL MOVED ON 2026-09-09, AND THIS SECTION ENFORCES THE NEW CONTRACT.
# It used to be a top-level `weight_goal_lb:` key, and an ITEM-level goal was
# deliberately REFUSED — a test asserted the refusal. That refusal is now
# reversed on the Owner's ruling, because the old design does not survive this
# household's configuration:
#
#   * the household runs Drive SHEET mode (TRACKER_DRIVE_SHEET_ID set,
#     TRACKER_DRIVE_FOLDER_ID blank), where internal/defsheet regenerates each
#     .md from a fixed ITEM-column list and drops unknown columns — so a
#     top-level key is ERASED by the first sync after any sheet edit;
#   * Google Health v4 carries no goal or target concept at all, so there is no
#     vendor fallback to lean on.
#
# `target` and `unit` are already item columns that round-trip today. The tests
# below therefore assert the OPPOSITE of what the old M10 test asserted, and
# `test_the_superseded_top_level_key_is_refused_not_silently_ignored_sr022`
# exists so nobody restores it by accident.
# ═══════════════════════════════════════════════════════════════════════════

def test_the_goal_is_read_from_the_owners_real_synced_file_sr022(tmp_path):
    """The goal comes off disk, out of the definitions NagLight also loads.

    HEALTH_MD is the shape the Owner's sheet ACTUALLY synced to the hub — the
    Health category, `color_weight: 1.5`, the weigh-in item carrying
    `target: 170` and `unit: lb`, `horizon: long`, and two sibling items either
    side of it — so this proves the reader handles the real file and not a
    fixture shaped to suit the parser.
    """
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD})
    goal, source = feeder.load_goal_from_definitions(defs_dir)
    assert goal == DEFS_GOAL
    assert source.endswith("health.md")


def test_the_goal_becomes_the_gauges_target_line_sr022(tmp_path):
    """SN-040: the goal IS the target line, and nothing else supplies it."""
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD})
    goal, _ = feeder.load_goal_from_definitions(defs_dir)
    body, _ = feeder.build_post((191.4, NOW), None, goal, NOW)
    assert body["target"] == 170.0
    assert body["value"] == 191.4


def test_the_siblings_targets_are_not_the_weight_goal_sr022(tmp_path):
    """The item is found by ID, not by "the first item with a target".

    A definitions file is full of items and other items have targets too — the
    step count next to the weigh-in is the obvious one. Picking by shape rather
    than by id would post a step goal as a body weight.
    """
    body = HEALTH_MD.replace("  - id: weigh-in\n", "  - id: not-weigh-in\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


def test_the_item_location_is_a_knob_so_the_owner_can_move_it_sr022(tmp_path):
    """The Owner must be able to rename or move the item without a code change."""
    body = HEALTH_MD.replace("category: Health", "category: Body")
    body = body.replace("  - id: weigh-in\n", "  - id: scale-day\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)
    goal, _ = feeder.load_goal_from_definitions(defs_dir, "Body", "scale-day")
    assert goal == DEFS_GOAL


def test_the_knobs_default_to_the_shape_the_owner_already_synced_sr022():
    """Defaults, and blanks that fall back to them, name the real item."""
    assert feeder.resolve_goal_location({}) == ("Health", "weigh-in")
    assert feeder.resolve_goal_location(
        {"WEIGHT_ITEM_CATEGORY": "", "WEIGHT_ITEM_ID": "   "}) == ("Health", "weigh-in")
    assert feeder.resolve_goal_location(
        {"WEIGHT_ITEM_CATEGORY": " Body ", "WEIGHT_ITEM_ID": " scale-day "}) \
        == ("Body", "scale-day")


def test_the_location_knobs_reach_the_loader_through_a_whole_cycle_sr022(tmp_path):
    """Wired end to end: a knob nobody reads is a knob that does not exist."""
    body = HEALTH_MD.replace("category: Health", "category: Body")
    seen = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": write_defs(tmp_path, {"health.md": body}),
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json"),
           "WEIGHT_ITEM_CATEGORY": "Body", "WEIGHT_ITEM_ID": "weigh-in"}
    feeder.run_cycle(env, now=NOW,
                     readers={"google-health": lambda e: (191.4, NOW)},
                     poster=lambda body_, *a: (seen.append(body_), (True, ""))[1])
    assert seen[-1]["target"] == DEFS_GOAL


def test_the_category_and_id_match_the_way_a_person_types_them_sr022(tmp_path):
    """Both halves are typed into a spreadsheet cell by a human.

    `Health` against `health` must not be the difference between a goal and a
    dark panel, so the match is case-insensitive and trimmed on both sides.
    """
    body = HEALTH_MD.replace("category: Health", "category: health")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    assert feeder.load_goal_from_definitions(defs_dir, "HEALTH", "Weigh-In")[0] \
        == DEFS_GOAL


# ── The unit is CHECKED, never assumed ─────────────────────────────────────
# This is the sharpest edge in the block. `target: 77` with `unit: kg` is
# 170 lb, and 77 sits INSIDE the 40..1000 lb sanity band — so the band does not
# catch it, and the panel would show "77 lb" against a real 191 lb reading and
# paint it full red. A wrong unit here is the same failure class as a vendor
# parser written from a schema: confident, plausible and wrong about a person's
# body, with nothing on the wall able to tell anyone.

@pytest.mark.parametrize("unit", ["kg", "kgs", "st", "lbs", "pounds", "%", '""'])
def test_a_unit_that_is_not_lb_is_refused_and_never_converted_sr022(tmp_path, unit):
    body = HEALTH_MD.replace("    unit: lb\n", "    unit: %s\n" % unit)
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "unit" in str(err.value)


def test_a_plausible_kilogram_target_is_refused_by_the_unit_not_the_band_sr022(tmp_path):
    """The case that motivates the whole check, asserted on its own.

    77 kg is a real goal a real person would type, and 77 is inside the pounds
    band, so the sanity band CANNOT catch it. If this test ever goes green with
    the unit check removed, the unit check is being carried by the band and the
    band does not actually cover this.
    """
    body = HEALTH_MD.replace("    target: 170\n", "    target: 77\n")
    body = body.replace("    unit: lb\n", "    unit: kg\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "kg" in str(err.value)


def test_a_target_with_no_unit_at_all_is_refused_sr022(tmp_path):
    """Absent is not "obviously pounds". Assuming is the whole defect."""
    body = HEALTH_MD.replace("    unit: lb\n", "")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "unit" in str(err.value)


def test_no_conversion_helper_is_reachable_from_the_goal_path_sr022(tmp_path):
    """There is deliberately NO kg->lb conversion on the goal path.

    `grams_to_pounds` exists for the vendor READING and is out of scope here;
    what must not exist is any path by which a non-`lb` target becomes a
    number. Asserted by proving every non-lb unit refuses (above) and that the
    goal reader never returns for one.
    """
    for unit, target in (("kg", "77"), ("st", "12"), ("g", "77000")):
        body = HEALTH_MD.replace("    target: 170\n", "    target: %s\n" % target)
        body = body.replace("    unit: lb\n", "    unit: %s\n" % unit)
        defs_dir = write_defs(tmp_path, {"health-%s.md" % unit: body})
        with pytest.raises(ValueError):
            feeder.load_goal_from_definitions(defs_dir)
        for name in os.listdir(defs_dir):
            os.unlink(os.path.join(defs_dir, name))


def test_the_unit_is_read_the_way_a_person_would_write_it(tmp_path):
    """Quotes and a trailing comment are ordinary in hand-edited frontmatter."""
    for written in ('"lb"', "'lb'", "lb  # pounds", "LB", " lb "):
        body = HEALTH_MD.replace("    unit: lb\n", "    unit: %s\n" % written)
        defs_dir = write_defs(tmp_path, {"health.md": body})
        assert feeder.load_goal_from_definitions(defs_dir)[0] == DEFS_GOAL


# ── The target itself ───────────────────────────────────────────────────────

def test_no_hub_knob_can_supply_the_goal_sr022(tmp_path):
    """The criterion is not merely "a goal exists" — it is WHERE it lives.

    A knob would be a second home for the household's intent that does not
    sync, so this asserts the negative: with a full environment set and an
    empty definitions tree, the cycle refuses. Every plausible knob name is
    tried, so adding one later turns this red. The two knobs this block DOES
    add name the item's LOCATION and are set here too — if either could ever
    carry a number, this test would stop meaning anything.
    """
    defs_dir = write_defs(tmp_path, {})
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": defs_dir,
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json"),
           "WEIGHT_ITEM_CATEGORY": "170", "WEIGHT_ITEM_ID": "170",
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


@pytest.mark.parametrize("replacement,why", [
    ("", "absent"),
    ("    target:\n", "blank"),
    ("    target: '   '\n", "whitespace"),
    ("    target: one seventy\n", "not a number"),
    ("    target: nan\n", "not finite"),
    ("    target: 1700\n", "out of band high"),
    ("    target: 17\n", "out of band low"),
    ("    target: 0\n", "zero"),
    ("    target: -170\n", "negative"),
])
def test_an_unusable_target_posts_nothing_at_all_sr022(tmp_path, replacement, why):
    """Blank, absent, non-numeric and out-of-band are ALL "no goal".

    Whether it surfaces as GoalMissing (nothing was declared) or ValueError
    (something was declared that cannot be used), the outcome the Owner sees is
    the same and is the one that matters: NOTHING IS POSTED, and the journal
    names the file. `1700` for `170` is the case that earns the band — it
    parses, it is finite, and it would drag NagLight's inferred bar to
    1675..1725 so every real reading paints full red forever.
    """
    posts = []
    body = HEALTH_MD.replace("    target: 170\n", replacement)
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": write_defs(tmp_path, {"health.md": body}),
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json")}
    with pytest.raises((feeder.GoalMissing, ValueError)):
        feeder.run_cycle(env, now=NOW,
                         readers={"google-health": lambda e: (191.4, NOW)},
                         poster=lambda b, *a: (posts.append(b), (True, ""))[1])
    assert posts == [], "%s target must post nothing at all" % why


def test_a_blank_target_is_MISSING_not_a_zero_sr022(tmp_path):
    """A blank target is "you have not said", not "you are aiming at nothing".

    ASSERTED ON THE EXCEPTION TYPE, THROUGH THE REAL LOADER, BECAUSE THE LOOSE
    VERSION SURVIVED ITS OWN MUTATION (M45) AND THAT WAS A TEST DEFECT. Turning
    a blank into `0` still posts nothing — the 40..1000 lb band catches the zero
    — so a test that only checks "nothing was posted" is satisfied by the BAND
    and says nothing about this line at all. The refusal has to be the MISSING
    one, or the journal tells the person their goal is out of range when what
    they have actually done is not set one.
    """
    body = HEALTH_MD.replace("    target: 170\n", "    target: '   '\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


@pytest.mark.parametrize("raw,expected", [
    ("170", 170.0), (" 170 ", 170.0), ("170.5", 170.5),
    ('"170"', 170.0), ("'170'", 170.0), ("170 # my goal", 170.0),
])
def test_the_target_is_parsed_the_way_a_person_would_write_it(tmp_path, raw, expected):
    """Frontmatter is hand-edited YAML; quotes and comments are ordinary."""
    body = HEALTH_MD.replace("    target: 170\n", "    target: %s\n" % raw)
    defs_dir = write_defs(tmp_path, {"health.md": body})
    assert feeder.load_goal_from_definitions(defs_dir)[0] == expected


# ── Reversing M10, without letting anyone reverse it back ──────────────────

def test_an_item_level_goal_is_now_THE_goal_which_reverses_m10_sr022(tmp_path):
    """M10 — "an item-level goal is refused" — is DELIBERATELY REVERSED.

    The old design refused this and a test enforced the refusal. It could not
    survive: in Drive sheet mode a top-level key is erased on the next sync,
    and Google Health v4 has no goal concept to fall back on. This test is the
    old one turned around, and it is named so that a reader who finds the old
    behaviour in the history knows the change was a ruling, not a regression.
    Why it changed is recorded in docs/status.md and at the head of the feeder.
    """
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD})
    item = feeder.find_goal_item(HEALTH_MD, "health.md", "Health", "weigh-in")
    assert item is not None and item["target"] == ["170"]
    assert feeder.load_goal_from_definitions(defs_dir)[0] == DEFS_GOAL


def test_the_superseded_top_level_key_is_refused_not_silently_ignored_sr022(tmp_path):
    """A file that still carries `weight_goal_lb` and nothing else is REFUSED.

    Not "no goal declared": a person upgrading would be staring at a
    `weight_goal_lb: 180` line while the journal said no goal was declared.
    The refusal names the key, says it is no longer read, and says where the
    number goes instead.
    """
    body = HEALTH_MD.replace("    target: 170\n", "")
    body = body.replace("color_weight: 1.5\n", "color_weight: 1.5\nweight_goal_lb: 180\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    message = str(err.value)
    assert "weight_goal_lb" in message and "NO LONGER READ" in message


def test_both_a_legacy_key_and_an_item_target_are_refused_not_ranked_sr022(tmp_path):
    """BOTH PRESENT IS A REFUSAL. Silent precedence is the trap.

    Whichever way it fell, the person would be looking at a bar drawn around
    one number while a different number sat in their file looking equally
    authoritative — and in sheet mode the top-level one is about to be deleted
    underneath them, so "the newest edit wins" is not even stable. Same rule
    this module already applies to two files declaring a goal.

    Asserted through a WHOLE CYCLE as well as at the loader, so the refusal is
    proved to cost the post and not merely to raise somewhere.
    """
    body = HEALTH_MD.replace("color_weight: 1.5\n",
                             "color_weight: 1.5\nweight_goal_lb: 180\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "REFUSED" in str(err.value)

    posts = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": defs_dir,
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json")}
    with pytest.raises(ValueError):
        feeder.run_cycle(env, now=NOW,
                         readers={"google-health": lambda e: (191.4, NOW)},
                         poster=lambda b, *a: (posts.append(b), (True, ""))[1])
    assert posts == []


def test_main_reports_the_refusal_and_exits_nonzero_without_posting_sr022(tmp_path, monkeypatch, capsys):
    """The refusal must be legible in `systemctl status`, not silent."""
    monkeypatch.setattr(feeder, "post_gauge",
                        lambda *a: pytest.fail("nothing may be posted"))
    monkeypatch.setattr(os, "environ", {
        "WEIGHT_ENABLED": "true", "WEIGHT_USER": "1080",
        "WEIGHT_FEED_URL": "http://127.0.0.1:8787/api/feed",
        "WEIGHT_DEFINITIONS_DIR": write_defs(tmp_path, {}),
        "WEIGHT_STATE_FILE": str(tmp_path / "state.json")})
    assert feeder.main([]) == 2
    err = capsys.readouterr().err
    assert "REFUSED, nothing posted" in err
    assert "weigh-in" in err          # it says WHICH item to put the target on


# ── The frontmatter reader, on its own ─────────────────────────────────────

def test_a_target_in_the_prose_body_is_not_the_goal_sr022(tmp_path):
    """Only the leading frontmatter counts, matching defs.frontmatter.

    THE FIRST VERSION OF THIS TEST SURVIVED ITS OWN MUTATION (M37) AND THAT WAS
    A TEST DEFECT, NOT A CODE ONE. It put a bare `- id:` block in the prose,
    which the parser skips anyway because a column-zero line ends the item
    sequence — so the closing fence was never what refused it, and deleting the
    fence check left the suite green. The prose here RE-OPENS `items:` at column
    zero, which is the only shape that actually reaches the item reader, and it
    declares the only weigh-in item in the file: with the fence honoured there
    is no goal, and without it there is a 999 lb one.
    """
    body = ("---\ncategory: Health\nitems:\n  - id: take-vitamins\n"
            "    title: Take the vitamins\n---\n"
            "\nNotes. Sketching the row I mean to add to the sheet:\n\n"
            "items:\n  - id: weigh-in\n    target: 999\n    unit: lb\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


def test_a_top_level_key_after_the_items_sequence_is_not_a_declaration_sr022():
    """internal/defs stops reading top-level scalars at `items:` and so do we.

    THIS TEST EXISTS BECAUSE THE MUTATION RUN FOUND THE BREAK UNTESTED: with it
    removed the whole suite stayed green, because the indent check downstream
    was catching the only case anything asserted.
    """
    body = ("---\ncategory: Health\nitems:\n  - id: weigh-in\n    target: 170\n"
            "    unit: lb\nweight_goal_lb: 999\n---\n")
    assert feeder.declares_legacy_goal(body) is False
    assert feeder.goal_from_item(
        feeder.find_goal_item(body, "f", "Health", "weigh-in"),
        "f", "Health", "weigh-in") == 170.0


def test_an_indented_legacy_key_is_an_item_field_not_a_top_level_one_sr022():
    """The indent half, asserted on its own.

    Written after the earlier mutation run, which showed the two guards were
    covering for each other so breaking either left the suite green.
    """
    assert feeder.declares_legacy_goal(
        "---\ncategory: Health\n  weight_goal_lb: 999\n---\n") is False
    assert feeder.declares_legacy_goal(
        "---\ncategory: Health\nweight_goal_lb: 999\n---\n") is True


# ── Depth: the four ways a nested block used to speak for the item ─────────
#
# EVERY FIXTURE BELOW IS THE CROSS-REVIEWER'S, VERBATIM, and every one of them
# used to yield a 170 lb goal from something that is not the Owner's declared
# target. They are one defect - the reader ignored indentation DEPTH, so
# anything shaped like `id:`/`target:`/`unit:` was read as a direct item field
# wherever it sat - and they are tested one by one because a single root-cause
# fix still has to be shown to close each door somebody actually pushed on.
#
# 170 IS DELIBERATELY THE SAME NUMBER THE OWNER'S REAL FILE CARRIES. A fixture
# that used 999 would be caught by a test asserting "not 999" even if the
# reader had merely stopped finding a goal for some unrelated reason; these
# assert on the SHAPE of the refusal instead.

NESTED_MAPPING_MD = """---
category: Health
items:
  - id: weigh-in
    metadata:
      target: 170
      unit: lb
---
"""

NESTED_LIST_MD = """---
category: Health
items:
  - id: take-vitamins
    alternatives:
      - id: weigh-in
        target: 170
        unit: lb
---
"""

SECOND_ITEMS_MD = """---
category: Health
items:
  - id: take-vitamins
    title: Take the vitamins
items:
  - id: weigh-in
    target: 170
    unit: lb
---
"""

TAB_INDENTED_MD = (
    "---\ncategory: Health\nitems:\n\t- id: weigh-in\n"
    "\t  target: 170\n\t  unit: lb\n---\n")


def test_a_nested_mapping_is_not_the_items_fields_sr022(tmp_path):
    """P1: `metadata: {target: 170, unit: lb}` is NOT a 170 lb goal.

    To every YAML parser alive that target belongs to `metadata`, and the item
    itself declares none. The old reader took both keys as the item's own and
    posted 170 lb against the Owner's real weight. The refusal that must come
    out is the one for an item with NO target - not a unit complaint, not a
    band complaint - because that is what the file actually says.
    """
    defs_dir = write_defs(tmp_path, {"health.md": NESTED_MAPPING_MD})
    with pytest.raises(feeder.GoalMissing) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "declares no `target:`" in str(err.value)


def test_a_nested_list_is_not_the_configured_item_sr022(tmp_path):
    """P2: an `alternatives:` list under take-vitamins is not the weigh-in item.

    The nested entry has the right id and the right two fields, so ANY reader
    that matches on shape rather than on depth finds it. The item the household
    configured does not exist in this file, and the honest answer is to say so.
    """
    defs_dir = write_defs(tmp_path, {"health.md": NESTED_LIST_MD})
    with pytest.raises(feeder.GoalMissing) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "weigh-in" in str(err.value)
    # ...and the nested entry did not become an ITEM either, which is the half
    # a "skip fields deeper than the item" patch alone would have left open.
    _top, items = feeder.parse_definitions_file(NESTED_LIST_MD, "health.md")
    assert len(items) == 1
    assert feeder.clean_scalar(items[0]["id"][0]) == "take-vitamins"


def test_a_second_items_key_is_refused_not_preferred_sr022(tmp_path):
    """P3: two `items:` blocks is an ambiguous document, not a reset.

    The old reader re-entered item mode on the second key and read the goal out
    of it. Merging them would be no better: which sequence the household meant
    is not guessable, and this module refuses that question everywhere else.
    """
    defs_dir = write_defs(tmp_path, {"health.md": SECOND_ITEMS_MD})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "not guessable" in str(err.value) and "items" in str(err.value)


def test_a_tab_indented_item_is_refused_not_trusted_sr022(tmp_path):
    """P4: YAML forbids tabs in indentation, so this file has no items at all.

    The loader that syncs these files rejects the whole document; a reader that
    accepted it would be the only thing in the household that believes it knows
    what the file declares - and what it would believe is a body weight.
    """
    defs_dir = write_defs(tmp_path, {"health.md": TAB_INDENTED_MD})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "TAB" in str(err.value)


@pytest.mark.parametrize("body", [NESTED_MAPPING_MD, NESTED_LIST_MD,
                                  SECOND_ITEMS_MD, TAB_INDENTED_MD])
def test_no_nesting_trick_can_reach_the_wall_sr022(tmp_path, body):
    """THE PROPERTY, not the four doors: NOTHING IS POSTED for any of them.

    Each fixture is run through a WHOLE CYCLE against a source that is working
    perfectly, because the hazard is not an exception in a unit test - it is
    170 lb arriving on the wall beside a real reading of 191. Whether the
    refusal is GoalMissing or ValueError is the message's business; that the
    poster is never called is the household's.
    """
    posted = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": write_defs(tmp_path, {"health.md": body}),
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json")}
    with pytest.raises((feeder.GoalMissing, ValueError)):
        feeder.run_cycle(env, now=NOW,
                         readers={"google-health": lambda _e: (191.4, NOW)},
                         poster=lambda b, *a: (posted.append(b), (True, ""))[1])
    assert posted == [], "a nested block put a goal on the wall"


def test_the_readers_narrowness_agrees_with_real_yaml_sr022():
    """The hand reader is narrow ON PURPOSE - but it must not be DIFFERENT.

    PyYAML is not a dependency of this service and never will be (stdlib only,
    no venv, and a new apt name means the offline apt export is re-run), but
    where it happens to be installed it is a free oracle: for each fixture, what
    the reader concluded about the weigh-in item's own `target` is checked
    against what a real YAML parser says. That is what keeps "narrow" from
    drifting into "wrong" without anybody noticing.
    """
    yaml = pytest.importorskip("yaml")

    def real_target(body):
        """The weigh-in item's OWN target, per PyYAML, or None."""
        front = "\n".join(feeder.frontmatter_lines(body))
        for item in (yaml.safe_load(front) or {}).get("items") or []:
            if isinstance(item, dict) and item.get("id") == "weigh-in":
                return item.get("target")
        return None

    assert real_target(HEALTH_MD) == 170              # the Owner's real file
    assert real_target(NESTED_MAPPING_MD) is None     # P1: it is metadata's
    assert real_target(NESTED_LIST_MD) is None        # P2: it is a nested list
    with pytest.raises(yaml.YAMLError):               # P4: not YAML at all
        yaml.safe_load("\n".join(feeder.frontmatter_lines(TAB_INDENTED_MD)))
    # P3 is a duplicate key: PyYAML's default loader keeps the LAST one
    # silently, which is precisely the "pick one" this reader refuses to do.
    assert real_target(SECOND_ITEMS_MD) == 170
    assert feeder.parse_definitions_file(HEALTH_MD, "health.md")[1][1]["target"] \
        == ["170"]
    for body in (NESTED_MAPPING_MD, NESTED_LIST_MD):
        item = feeder.find_goal_item(body, "health.md", "Health", "weigh-in")
        assert item is None or "target" not in item


def test_a_deeper_block_cannot_reopen_the_item_after_a_nested_one_sr022():
    """Depth is tracked, not "have we seen a nested key yet".

    `notes:` opens a nested block; the item's OWN `unit:` comes back at the
    item's column afterwards and must still be read, or the fix would be a
    different silent wrongness - a real target refused for want of a unit.
    """
    body = ("---\ncategory: Health\nitems:\n  - id: weigh-in\n"
            "    target: 170\n    notes:\n      unit: kg\n      target: 77\n"
            "    unit: lb\n---\n")
    item = feeder.find_goal_item(body, "health.md", "Health", "weigh-in")
    assert item["target"] == ["170"] and item["unit"] == ["lb"]
    assert feeder.goal_from_item(item, "health.md", "Health", "weigh-in") == 170.0


def test_an_item_written_with_the_dash_on_its_own_line_still_reads_sr022():
    """`-` alone, fields below it, is the other block form a person may type."""
    body = ("---\ncategory: Health\nitems:\n  -\n    id: weigh-in\n"
            "    target: 170\n    unit: lb\n---\n")
    item = feeder.find_goal_item(body, "health.md", "Health", "weigh-in")
    assert feeder.goal_from_item(item, "health.md", "Health", "weigh-in") == 170.0


def test_a_sequence_at_column_zero_is_still_the_items_sequence_sr022():
    """YAML lets a sequence sit at its key's own indent, and people write it.

    The old reader treated every column-zero line as a top-level key, so this
    entirely valid file silently held NO items - a blind spot that ends in "no
    goal" for a household that declared one perfectly legibly.
    """
    body = ("---\ncategory: Health\nitems:\n- id: weigh-in\n  target: 170\n"
            "  unit: lb\n---\n")
    item = feeder.find_goal_item(body, "health.md", "Health", "weigh-in")
    assert feeder.goal_from_item(item, "health.md", "Health", "weigh-in") == 170.0


def test_an_inline_items_sequence_is_refused_rather_than_guessed_at_sr022():
    """Flow style is not the shape the sheet generates, so it is not read."""
    body = ("---\ncategory: Health\nitems: [{id: weigh-in, target: 170}]\n---\n")
    with pytest.raises(ValueError) as err:
        feeder.parse_definitions_file(body, "health.md")
    assert "inline sequence" in str(err.value)


def test_a_definitions_file_that_links_out_of_the_directory_is_refused_sr022(tmp_path):
    """PRIORITY 4, and the cheap half of it.

    The definitions directory is the tracker's own docker volume and so is
    inside the trust boundary - WEIGHT_DEFINITIONS_DIR may legitimately BE a
    symlink, and that stays supported. What is refused is an entry that leaves
    it: "the goal came from a file that is not in the household's definitions"
    is a sentence this feeder should not be able to say.
    """
    outside = tmp_path / "elsewhere.md"
    outside.write_text(HEALTH_MD.replace("target: 170", "target: 199"),
                       encoding="utf-8")
    defs_dir = write_defs(tmp_path, {})
    try:
        os.symlink(str(outside), os.path.join(defs_dir, "health.md"))
    except (OSError, NotImplementedError):    # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "outside the definitions directory" in str(err.value)


def test_a_symlinked_definitions_directory_is_still_read_sr022(tmp_path):
    """The other side of the same coin: the docker volume itself may be a link,
    and refusing that would be refusing the normal deployment."""
    real = write_defs(tmp_path, {"health.md": HEALTH_MD})
    link = str(tmp_path / "definitions-link")
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):    # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")
    assert feeder.load_goal_from_definitions(link)[0] == DEFS_GOAL


def test_two_files_holding_the_item_are_refused_not_ordered_sr022(tmp_path):
    """Picking the first would silently follow file-name order.

    That is the same defect class defs.Load found on a real household's files
    with duplicate `check:` ids: a loser that is never used and never reported.
    """
    other = HEALTH_MD.replace("    target: 170", "    target: 165")
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD, "aaa.md": other})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "not guessable" in str(err.value)


def test_one_file_holding_the_item_twice_is_refused_sr022(tmp_path):
    """Two items with the same id in one file: also not guessable."""
    doubled = HEALTH_MD.replace(
        "  - id: weigh-in\n    title: Step on the scale\n",
        "  - id: weigh-in\n    title: Step on the scale\n"
        "    unit: lb\n    target: 165\n  - id: weigh-in\n    title: Again\n")
    defs_dir = write_defs(tmp_path, {"health.md": doubled})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "not guessable" in str(err.value)


def test_a_target_declared_twice_on_the_item_is_refused_sr022(tmp_path):
    """Last-one-wins on a body-weight goal is a number nobody chose."""
    body = HEALTH_MD.replace("    target: 170\n", "    target: 170\n    target: 165\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "not guessable" in str(err.value)


def test_a_file_that_is_not_a_definitions_file_is_skipped_not_read_sr022(tmp_path):
    """A stray .md beside the definitions must not be parsed for items."""
    assert feeder.parse_definitions_file("just some notes\n") is None
    defs_dir = write_defs(tmp_path, {"notes.md": "no frontmatter here\n",
                                     "health.md": HEALTH_MD})
    assert feeder.load_goal_from_definitions(defs_dir)[0] == DEFS_GOAL


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
# The vendor path: the gate is CLEARED, and the parser is asserted correct
#
# Until 2026-09-09 this section asserted that NO parser existed, because no
# authenticated call had ever been made (B7's `parse_gemini` precedent). The
# Owner then ran `weight_oauth.py capture` against the live API and got HTTP
# 200 with one data point, so the absence assertions have been REPLACED - not
# quietly dropped - by these, which assert the parser's correctness against the
# SHAPE of that body. docs/status.md carries the record of the clearing.
#
# EVERY FIXTURE BELOW IS SYNTHETIC. The captured body carries the Owner's
# Google user id and their real body weight; neither is in this repo. The ids
# here are obvious placeholders and the weights are made up.
# ═══════════════════════════════════════════════════════════════════════════

# A sentinel standing where the Owner's Google user id sat. Nothing this parser
# produces, stores or says may ever contain it - which is a property a test can
# check by looking for this exact string.
SENTINEL_ID = "PLACEHOLDER-USER-ID-NOT-THE-OWNERS"

# What a hostile or confused remote puts in an error body, so "the body is not
# logged" can be asserted as an ABSENCE rather than as a phrase in a sentence.
ERROR_BODY_SENTINEL = "REMOTE-CHOSE-THESE-BYTES-4417"

# 79 832 g is 176.0 lb to one decimal, the precision the panel shows. It is NOT
# the captured weight.
FIXTURE_GRAMS = 79832
FIXTURE_POUNDS = FIXTURE_GRAMS / 453.59237

# The captured body's timestamps, kept EXACTLY, because they are the proof of
# the civilTime/physicalTime trap: 01:24 on the 9th in UTC is 20:24 on the 8th
# locally, five hours west. The weigh-in was a Monday evening; in UTC it is
# Tuesday.
FIXTURE_PHYSICAL = "2026-09-09T01:24:33.390135Z"
FIXTURE_OFFSET = "-18000s"
FIXTURE_CIVIL_DAY = (2026, 9, 8)
FIXTURE_UTC_DAY = "2026-09-09"
CAPTURED_AT = 1788917073            # what FIXTURE_PHYSICAL is, in epoch seconds


def a_point(physical=FIXTURE_PHYSICAL, grams=FIXTURE_GRAMS,
            offset=FIXTURE_OFFSET, civil=True, point_id="P1"):
    """One `dataPoints` element in the shape observed on 2026-09-09."""
    sample = {"physicalTime": physical, "utcOffset": offset}
    if civil:
        sample["civilTime"] = {
            "date": {"year": 2026, "month": 9, "day": 8},
            "time": {"hours": 20, "minutes": 24, "seconds": 33,
                     "nanos": 390135000}}
    return {
        "name": "users/%s/dataTypes/weight/dataPoints/%s" % (SENTINEL_ID, point_id),
        "dataSource": {"recordingMethod": "MANUAL", "platform": "FITBIT"},
        "weight": {"sampleTime": sample, "weightGrams": grams},
    }


def a_body(*points, **extra):
    """A `ListDataPointsResponse` around those points."""
    body = {"dataPoints": list(points)}
    body.update(extra)
    return body


CAPTURED_SHAPE = a_body(a_point())


def _json_200(handler, payload):
    body = json.dumps(payload).encode()
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def paged_google(pages):
    """A loopback responder serving `{path: payload}` and refusing anything else."""
    def respond(handler):
        _json_200(handler, pages[handler.path])
    return respond


def fake_google(list_payload, token_payload=None, list_status=200):
    """A loopback stand-in for Google's token endpoint AND the health API.

    `/token` answers the refresh grant; anything else is the dataPoints list.
    """
    payloads = {"token": token_payload or {"access_token": "ACCESS-TOKEN-XYZ"},
                "list": list_payload}

    def respond(handler):
        if handler.path.startswith("/token"):
            _json_200(handler, payloads["token"])
            return
        if list_status != 200:
            # A REAL ERROR BODY, carrying a sentinel. Google's error bodies have
            # been observed quoting the offending request back, and on this path
            # that request carries the whole health-metrics scope in a header.
            # An empty body here would let a feeder that echoes the remote's
            # bytes pass unnoticed.
            error = json.dumps({"error": {"message": ERROR_BODY_SENTINEL}}).encode()
            handler.send_response(list_status)
            handler.send_header("Content-Length", str(len(error)))
            handler.end_headers()
            handler.wfile.write(error)
            return
        _json_200(handler, payloads["list"])

    return respond


def token_file(tmp_path, refresh="REFRESH-TOKEN-ABC"):
    path = tmp_path / "google-health-token.json"
    path.write_text(json.dumps({"refresh_token": refresh, "token_type": "Bearer"}),
                    encoding="utf-8")
    return path


def google_env(tmp_path, token_path=None):
    return {"WEIGHT_TOKEN_FILE": str(token_path or token_file(tmp_path)),
            "OAUTH2_PROXY_CLIENT_ID": "client-id",
            "OAUTH2_PROXY_CLIENT_SECRET": "client-secret",
            "WEIGHT_TIMEOUT_SECONDS": "10",
            "_now": NOW}


def test_the_captured_shape_parses_to_the_weight_and_the_instant_sr022():
    """The whole gate, in one assertion: the observed body yields the right
    number of pounds at the instant the reading was true.

    `weightGrams` is GRAMS. This is the fact one real call was demanded for,
    because a parser that read it as kilograms or as pounds would not fail - it
    would post a confident, plausible, WRONG body weight that nothing on the
    wall could contradict.
    """
    reading = feeder.parse_weight_datapoint(CAPTURED_SHAPE, NOW)
    assert reading.observed_at == CAPTURED_AT
    assert feeder.iso8601_utc(reading.observed_at) == "2026-09-09T01:24:33Z"
    assert round(reading.pounds, 1) == 176.0
    assert abs(reading.pounds - FIXTURE_POUNDS) < 1e-9


def test_grams_are_not_kilograms_and_not_pounds_sr022():
    """The two mutants this parser exists to kill, asserted as NUMBERS.

    Each wrong reading is stated as the value it WOULD produce, so a mutant
    fails on the number rather than merely on a different exception. 79 832 read
    as kilograms is 175 999 lb; read as pounds it is 79 832 lb; read as grams it
    is 176.0 lb. Only one of the three is a body.
    """
    reading = feeder.parse_weight_datapoint(CAPTURED_SHAPE, NOW)
    assert round(reading.pounds, 1) == 176.0
    assert round(reading.pounds, 1) != round(FIXTURE_GRAMS * 2.20462, 1)
    assert round(reading.pounds, 1) != float(FIXTURE_GRAMS)
    assert round(reading.pounds, 1) != round(FIXTURE_GRAMS / 1000.0, 1)
    # ...and the band is the SECOND line, not the first: it catches the two
    # gross mutants, and is asserted here to be doing so rather than assumed.
    with pytest.raises(feeder.SourceFailure):
        feeder.check_plausible_weight_lb(FIXTURE_GRAMS * 2.20462, "kg mutant")
    with pytest.raises(feeder.SourceFailure):
        feeder.check_plausible_weight_lb(float(FIXTURE_GRAMS), "lb mutant")


def test_the_latest_reading_wins_and_array_order_is_not_trusted_sr022():
    """"The first element" is not "the newest", and the observed body could not
    tell the difference: it held exactly one point.

    The three points here are deliberately scrambled - the newest sits in the
    MIDDLE - and each carries a distinct weight, so picking by position gives a
    different, wrong, entirely plausible body weight.
    """
    body = a_body(
        a_point("2026-09-01T12:00:00Z", 70000, point_id="old"),
        a_point("2026-09-09T01:24:33.390135Z", FIXTURE_GRAMS, point_id="new"),
        a_point("2026-09-05T12:00:00Z", 75000, point_id="middling"))
    reading = feeder.parse_weight_datapoint(body, NOW)
    assert reading.observed_at == CAPTURED_AT
    assert round(reading.pounds, 1) == 176.0
    assert round(reading.pounds, 1) != round(70000 / 453.59237, 1)
    assert round(reading.pounds, 1) != round(75000 / 453.59237, 1)


def test_the_calendar_day_is_civil_and_never_the_utc_day_sr022():
    """THE TRAP, PROVEN BY THE CAPTURE AND ASSERTED HERE.

    `physicalTime` is 01:24 on the 9th; `civilTime` is 20:24 on the 8th. A
    future auto-check-off that asked `observed_at` what day it was would tick
    off Tuesday for a Monday-evening weigh-in, every time. The parser must
    expose the civil day rather than throw it away, and it must not be the UTC
    day.
    """
    reading = feeder.parse_weight_datapoint(CAPTURED_SHAPE, NOW)
    assert reading.civil_date == FIXTURE_CIVIL_DAY
    assert feeder.iso8601_utc(reading.observed_at).startswith(FIXTURE_UTC_DAY)
    assert reading.civil_date[2] != int(FIXTURE_UTC_DAY.split("-")[2]), (
        "the civil day and the UTC day must differ in this fixture, or this "
        "test is not exercising the trap at all")
    assert reading.utc_offset_seconds == -18000


def test_the_offset_fallback_agrees_with_the_servers_own_civil_time():
    """`civilTime` is readOnly - the server computes it - so the arithmetic
    fallback for a body that omits it is trustable only if the two agree on the
    one body anybody has seen. They do."""
    assert (feeder.parse_weight_datapoint(a_body(a_point(civil=False)),
                                          NOW).civil_date == FIXTURE_CIVIL_DAY)


def test_an_unusable_offset_gives_no_day_rather_than_the_wrong_day():
    """None means "we cannot say", and it is deliberately not zero: zero is UTC,
    a real offset, and answering it for an unreadable field would place the
    weigh-in on the wrong calendar day with total confidence."""
    for bad in ("18000", "-18000 s", "", None, 42, "99999999s"):
        assert feeder.parse_google_duration_seconds(bad) is None
    reading = feeder.parse_weight_datapoint(
        a_body(a_point(offset="not-a-duration", civil=False)), NOW)
    assert reading.utc_offset_seconds is None
    assert reading.civil_date is None
    # ...and the READING still stands. A timezone this parser cannot read is
    # not a reason to refuse a weight that has a real instant.
    assert round(reading.pounds, 1) == 176.0


def test_an_empty_history_is_the_unavailable_gauge_and_not_a_broken_source():
    """An account with no weight logged returns 200 with no points.

    That is "we do not know what you weigh" - a DIFFERENT sentence from "the
    source is broken" and the SAME outcome. So it is a distinct exception TYPE
    that is a SourceFailure SUBCLASS: the subclassing keeps the unavailable path
    intact, and the distinct type stops a human reading "your token expired" as
    "you have never weighed yourself".
    """
    assert issubclass(feeder.NoWeightYet, feeder.SourceFailure)
    for empty in (a_body(), {"dataPoints": None}):
        with pytest.raises(feeder.NoWeightYet) as err:
            feeder.parse_weight_datapoint(empty, NOW)
        assert "no weight logged" in str(err.value)


def test_an_absent_datapoints_key_is_refused_and_is_not_an_empty_history():
    """`{}` and `{"dataPoints": []}` look alike and mean opposite things.

    The captured body carried the key, so a body without it is a SHAPE THIS
    PARSER DOES NOT RECOGNISE - the response changed, or something that is not
    Google answered. Reading "no weight logged" out of that would be reading
    meaning into a body nobody has ever seen.
    """
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.parse_weight_datapoint({}, NOW)
    assert not isinstance(err.value, feeder.NoWeightYet), (
        "an absent key must not be reported as an empty history")
    assert "no `dataPoints` key at all" in str(err.value), (
        "the message must name the missing key; `dataPoints` alone appears in "
        "several other refusals and would be carried by any of them")
    # And the SINGULAR spelling this repo's prose once guessed is not accepted.
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.parse_weight_datapoint({"dataPoint": [a_point()]}, NOW)
    assert not isinstance(err.value, feeder.NoWeightYet)


@pytest.mark.parametrize("broken,why", [
    ({"name": "x", "dataSource": {}}, "no weight member"),
    ({"weight": {"sampleTime": {"physicalTime": FIXTURE_PHYSICAL}}}, "no weightGrams"),
    ({"weight": {"weightGrams": FIXTURE_GRAMS}}, "no sampleTime"),
    ({"weight": {"sampleTime": {}, "weightGrams": FIXTURE_GRAMS}}, "no physicalTime"),
    ({"weight": {"sampleTime": {"physicalTime": "the 9th"},
                 "weightGrams": FIXTURE_GRAMS}}, "unparseable physicalTime"),
    ({"weight": {"sampleTime": {"physicalTime": "2026-09-09T01:24:33"},
                 "weightGrams": FIXTURE_GRAMS}}, "zoneless physicalTime"),
    ({"weight": "not an object"}, "weight is not an object"),
    ("not an object", "the point is not an object"),
])
def test_each_malformed_point_alone_is_a_named_failure_and_never_a_reading(broken, why):
    """Every shape the body could take that is not a usable weight, one case
    each, and each ALONE in the body - so nothing else can be carrying the
    refusal on its behalf."""
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.parse_weight_datapoint(a_body(broken), NOW)
    assert not isinstance(err.value, feeder.NoWeightYet), why
    assert "not one usable weight" in str(err.value)


@pytest.mark.parametrize("grams", [
    None, "176", True, False, float("nan"), float("inf"), float("-inf"),
    -1.0, 0, 0.0, -79832, 1e12, 0.5, [], {},
])
def test_a_weightgrams_that_is_not_a_body_weight_is_refused(grams):
    """Non-numeric, non-finite, negative, zero and absurd, each alone.

    `True` is in the list because `isinstance(True, int)` is True in Python and
    a bool would otherwise sail through as 1 gram - 0.002 lb, which only the
    band would then catch. Both guards are asserted rather than one relied on
    to cover the other.
    """
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_weight_datapoint(a_body(a_point(grams=grams)), NOW)


def test_a_future_sample_time_is_refused_because_it_would_fabricate_freshness():
    """A stamp ahead of the clock keeps a dead source rendering green until the
    stamp itself expires. It is refused, never clamped to `now`: clamping would
    invent exactly the thing the freshness invariant forbids."""
    ahead = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 7 * 24 * 3600))
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_weight_datapoint(a_body(a_point(physical=ahead)), NOW)


def test_one_broken_point_costs_itself_and_not_the_whole_reading():
    """A malformed entry in a synced history must not blank the panel."""
    body = a_body({"weight": {"weightGrams": "not a number"}},
                  a_point(),
                  {"name": "users/%s/..." % SENTINEL_ID})
    reading = feeder.parse_weight_datapoint(body, NOW)
    assert reading.observed_at == CAPTURED_AT
    assert round(reading.pounds, 1) == 176.0


def test_two_newest_points_that_disagree_are_refused_rather_than_ranked():
    """The rule this module already applies to two files declaring a goal, two
    items with one id, and a target declared twice. Identical weights at one
    instant are a duplicate and are accepted; different ones are a
    contradiction, and picking one would draw a bar around a number the other
    reading denies."""
    contradiction = a_body(a_point(grams=FIXTURE_GRAMS, point_id="a"),
                           a_point(grams=FIXTURE_GRAMS + 5000, point_id="b"))
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.parse_weight_datapoint(contradiction, NOW)
    assert "not guessable" in str(err.value)
    duplicate = a_body(a_point(point_id="a"), a_point(point_id="b"))
    assert round(feeder.parse_weight_datapoint(duplicate, NOW).pounds, 1) == 176.0


def test_no_vendor_value_and_no_user_id_ever_reaches_a_message_sr022():
    """THE NO-LEAK PROPERTY, asserted over every refusal this parser can make.

    `run_cycle` prints a SourceFailure's message and systemd writes it to the
    journal, so a message that quoted the body would persist the Owner's Google
    user id - or their body weight - to disk. `name` is never read at all,
    which is why the sentinel cannot appear; numbers are described by TYPE
    rather than by value.
    """
    leaky = "SENTINEL-LEAK-8891"
    bodies = [
        a_body(a_point(grams=leaky)),
        a_body(a_point(physical=leaky)),
        a_body({"weight": leaky}),
        a_body(a_point(grams=FIXTURE_GRAMS * 1000)),
        {"dataPoints": leaky},
        leaky,
    ]
    for body in bodies:
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.parse_weight_datapoint(body, NOW)
        message = str(err.value)
        assert leaky not in message, message
        assert SENTINEL_ID not in message, message
    # A SUCCESSFUL parse keeps the user id out of everything it returns,
    # including the repr a traceback would print - and the repr must not carry
    # the weight or the stamp either, because those are health data too.
    reading = feeder.parse_weight_datapoint(CAPTURED_SHAPE, NOW)
    assert not hasattr(reading, "name")
    for slot in feeder.WeightReading.__slots__:
        assert SENTINEL_ID not in str(getattr(reading, slot))
    assert SENTINEL_ID not in repr(reading)
    assert "176" not in repr(reading) and str(CAPTURED_AT) not in repr(reading)


def test_a_next_page_token_is_read_and_an_absent_one_is_none():
    """The observed response had NO `nextPageToken`. What this feeder DOES with
    one is stated in `list_weight_data_points` and marked there as an
    assumption; this asserts only the reading of the field."""
    assert feeder.next_page_token(a_body(a_point())) is None
    assert feeder.next_page_token(a_body(a_point(), nextPageToken="")) is None
    assert feeder.next_page_token(a_body(a_point(), nextPageToken=7)) is None
    assert feeder.next_page_token(a_body(a_point(), nextPageToken="ct-2")) == "ct-2"


def test_a_paged_history_is_walked_and_the_latest_across_pages_wins():
    """ASSUMPTION, NOT OBSERVATION. A page token is followed, because the
    response promises no ordering and a page left unwalked could hold a newer
    weigh-in than any seen - so "the latest" would otherwise be a claim this
    code cannot support. The newest point sits on the LAST page on purpose."""
    pages = {
        "/": a_body(a_point("2026-09-01T12:00:00Z", 70000), nextPageToken="p2"),
        "/?pageToken=p2": a_body(a_point("2026-09-05T12:00:00Z", 75000),
                                 nextPageToken="p3"),
        "/?pageToken=p3": a_body(a_point()),
    }
    with loopback_server(paged_google(pages)) as (base, seen):
        reading = feeder.list_weight_data_points("access-token", NOW, 10,
                                                 base + "/")
    assert [request["path"] for request in seen] == list(pages)
    assert reading.observed_at == CAPTURED_AT
    assert round(reading.pounds, 1) == 176.0


def test_a_history_deeper_than_the_page_cap_is_refused_rather_than_guessed():
    """A walk that never ends cannot claim to have found the latest reading, so
    it refuses. The cost is an unavailable gauge, which re-posts the last real
    reading at its ORIGINAL stamp - the panel degrades to the previous truth
    rather than to a number this code cannot vouch for."""
    def respond(handler):
        _json_200(handler, a_body(a_point(), nextPageToken="always"))

    with loopback_server(respond) as (base, seen):
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.list_weight_data_points("access-token", NOW, 10, base + "/")
    assert not isinstance(err.value, feeder.NoWeightYet)
    assert "will not claim one" in str(err.value)
    assert len(seen) == feeder.MAX_LIST_PAGES


def test_an_empty_page_mid_walk_is_not_an_empty_history():
    """`NoWeightYet` from one page must not end the walk - and an account whose
    every page is empty must still be `NoWeightYet` rather than a crash."""
    pages = {"/": a_body(nextPageToken="p2"), "/?pageToken=p2": a_body(a_point())}
    with loopback_server(paged_google(pages)) as (base, _seen):
        assert feeder.list_weight_data_points(
            "access-token", NOW, 10, base + "/").observed_at == CAPTURED_AT

    with loopback_server(paged_google({"/": a_body()})) as (base, _seen):
        with pytest.raises(feeder.NoWeightYet):
            feeder.list_weight_data_points("access-token", NOW, 10, base + "/")


def test_the_whole_vendor_read_returns_the_weight_and_the_instant(tmp_path):
    """End to end over real sockets: token refresh, list call, one reading.

    The WHOLE WeightReading comes back - `reading_pair` takes the gauge's two
    numbers off it - and `observed_at` is the sample's own instant, never
    `now`. The DAY FACTS must survive this call too: the check-off reaches the
    vendor only through this function, and if they were dropped here it would
    either tick on the wrong day or have to make a second, token-carrying call
    to recover them. Exactly two requests reach the loopback, which is what
    proves it did not.
    """
    env = google_env(tmp_path)
    with loopback_server(fake_google(CAPTURED_SHAPE)) as (base, seen):
        reading = feeder.read_google_health(
            env, list_url=base + "/points", token_endpoint=base + "/token")
    value, observed_at = feeder.reading_pair(reading)
    assert round(value, 1) == 176.0
    assert observed_at == CAPTURED_AT
    assert observed_at != NOW, "observed_at is when the reading was TRUE"
    assert feeder.reading_day_facts(reading) == (FIXTURE_CIVIL_DAY, -18000), (
        "the calendar day and its offset survive the vendor read")
    assert len(seen) == 2, "one token refresh and one list call, and no more"
    refresh, listing = seen
    assert b"grant_type=refresh_token" in refresh["body"]
    assert b"REFRESH-TOKEN-ABC" in refresh["body"]
    assert listing["headers"]["Authorization"] == "Bearer ACCESS-TOKEN-XYZ"
    assert listing["path"] == "/points", (
        "no query parameter is sent: the only call ever observed sent none")


def test_the_vendor_read_never_writes_the_token_file(tmp_path):
    """The credential write guard, from the side the parser opened.

    `read_google_health` is now the one function that opens the refresh token,
    and a read path that grew a write would be the exact thing this feeder
    promises it cannot do. The file's bytes and its mtime are both asserted.
    """
    path = token_file(tmp_path)
    before, before_mtime = path.read_bytes(), path.stat().st_mtime
    with loopback_server(fake_google(CAPTURED_SHAPE)) as (base, _seen):
        feeder.read_google_health(google_env(tmp_path, path),
                                  list_url=base + "/points",
                                  token_endpoint=base + "/token")
    assert path.read_bytes() == before
    assert path.stat().st_mtime == before_mtime
    # And the write guard still refuses that path by name, as it always did.
    with pytest.raises(PermissionError):
        feeder.open_for_write(str(path), str(path), str(tmp_path))


def test_the_vendor_read_refuses_a_redirect_and_the_target_gets_nothing(tmp_path):
    """urllib carries `Authorization` across a cross-host redirect, and this
    token grants blood glucose, body fat, oxygen saturation, core temperature
    and heart-rate metrics as well as weight. The second server must record no
    request AT ALL - asserted, not assumed."""
    with loopback_server(plain_200) as (elsewhere, arrived):
        def respond(handler):
            if handler.path.startswith("/token"):
                _json_200(handler, {"access_token": "ACCESS-TOKEN-XYZ"})
                return
            handler.send_response(302)
            handler.send_header("Location", elsewhere + "/points")
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        with loopback_server(respond) as (base, _seen):
            with pytest.raises(feeder.SourceFailure) as err:
                feeder.read_google_health(google_env(tmp_path),
                                          list_url=base + "/points",
                                          token_endpoint=base + "/token")
        assert arrived == []
    # NAME THE REDIRECT, NOT THE WORD "refused". If the redirect were FOLLOWED,
    # the second server answers `{}` and the parser's absent-`dataPoints`
    # refusal ALSO contains the word "refused" - so a substring test for it
    # would pass on the exact behaviour this test exists to forbid. The status
    # code can only come from the redirect refusal.
    message = str(err.value)
    assert "302 redirect" in message
    assert "may not be re-sent" in message


def test_a_non_200_from_the_list_call_never_logs_the_remotes_body(tmp_path):
    """A Google error body has been observed quoting the offending request back,
    and on this path that request carries the whole health-metrics scope."""
    with loopback_server(fake_google(CAPTURED_SHAPE, list_status=401)) as (base, _s):
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.read_google_health(google_env(tmp_path),
                                      list_url=base + "/points",
                                      token_endpoint=base + "/token")
    message = str(err.value)
    assert "HTTP 401" in message
    assert "mint --force" in message
    # THE ABSENCE IS THE ASSERTION. "body is not logged" is a phrase this repo
    # writes and would survive a feeder that appended the body after it, so the
    # test looks for the remote's own bytes and does not find them.
    assert ERROR_BODY_SENTINEL not in message
    assert "body is not logged" in message


def test_the_vendor_read_refuses_by_name_when_there_is_no_token(tmp_path):
    """Each missing piece has a DIFFERENT errand, so each gets its own sentence.

    Returning a placeholder reading, or None, would post a number; raising the
    wrong exception type would crash the cycle before the unavailable gauge was
    posted. Both are asserted against, as they were while this half was blocked.
    """
    for env, expected in [({}, "WEIGHT_TOKEN_FILE is unset"),
                          ({"WEIGHT_TOKEN_FILE": str(tmp_path / "nope.json")},
                           "mint")]:
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.read_google_health(dict(env, _now=NOW))
        assert expected in str(err.value)
    empty = tmp_path / "google-health-token.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.read_google_health({"WEIGHT_TOKEN_FILE": str(empty), "_now": NOW})
    assert "no `refresh_token`" in str(err.value)


def test_the_refresh_token_never_reaches_a_message_on_any_failure(tmp_path):
    """The credential this feeder reads must not be printable by any refusal.

    `run_cycle` puts a SourceFailure's message in the journal, and this token
    grants blood glucose, body fat, oxygen saturation, core temperature and
    heart-rate metrics as well as weight. Every failure the read can reach is
    driven here and the token is looked for in each message - an absence, not a
    phrase.
    """
    secret = "REFRESH-TOKEN-SENTINEL-2291"
    path = token_file(tmp_path, refresh=secret)
    messages = []

    # EVERY FIXTURE ON A FAILING PATH MUST ITSELF CARRY THE SECRET, or the
    # test proves nothing about that path. The first cut of this test used a
    # blank `{}` token file and a sentinel-free token response, and the mutation
    # run caught it: two mutants that printed `stored` and `payload` verbatim
    # SURVIVED, because there was nothing in either for them to print.
    broken = tmp_path / "broken.json"
    broken.write_text("{" + secret, encoding="utf-8")           # not JSON
    wrong_key = tmp_path / "wrong-key.json"
    wrong_key.write_text(json.dumps({"refreshToken": secret}), encoding="utf-8")
    for path_under_test in (broken, wrong_key, tmp_path / "gone.json"):
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.read_google_health({"WEIGHT_TOKEN_FILE": str(path_under_test),
                                       "_now": NOW})
        messages.append(str(err.value))

    def no_access_token(handler):
        # A token response with NO access_token and the secret inside it, so a
        # refusal that echoed the payload would be visible here.
        _json_200(handler, {"error": "invalid_grant", "echoed": secret})

    with loopback_server(no_access_token) as (base, _seen):
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.read_google_health(google_env(tmp_path, path),
                                      list_url=base + "/points",
                                      token_endpoint=base + "/token")
        messages.append(str(err.value))

    with loopback_server(fake_google(CAPTURED_SHAPE, list_status=403)) as (base, _s):
        with pytest.raises(feeder.SourceFailure) as err:
            feeder.read_google_health(google_env(tmp_path, path),
                                      list_url=base + "/points",
                                      token_endpoint=base + "/token")
        messages.append(str(err.value))

    assert len(messages) == 5
    for message in messages:
        assert secret not in message, message
        assert "ACCESS-TOKEN" not in message, message
        assert ERROR_BODY_SENTINEL not in message, message


def test_a_missing_oauth_client_names_every_variable_and_no_value(tmp_path):
    """The refusal that was hard to diagnose on the real hub was the one naming
    only the variables that do not exist. This one names all four, says which
    are set, and prints no value."""
    env = {"WEIGHT_TOKEN_FILE": str(token_file(tmp_path)),
           "OAUTH2_PROXY_CLIENT_ID": "an-id-that-must-not-be-printed",
           "_now": NOW}
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.read_google_health(env)
    message = str(err.value)
    for pair in feeder.OAUTH_CLIENT_KEY_PAIRS:
        for key in pair:
            assert key in message
    assert "an-id-that-must-not-be-printed" not in message


def test_the_feeder_refreshes_with_the_same_client_the_mint_used():
    """The feeder cannot import `weight_oauth` (which imports IT), so the
    resolution order and the token endpoint are duplicated. Duplicated is fine;
    DIVERGENT would be a token refreshed with a client it was not minted by,
    which Google answers `invalid_client` and which reads like Google's fault."""
    spec = importlib.util.spec_from_file_location(
        "weight_oauth_parity", REPO / "stack" / "weight" / "weight_oauth.py")
    oauth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oauth)
    assert feeder.OAUTH_CLIENT_KEY_PAIRS == oauth.CLIENT_KEY_PAIRS
    assert feeder.GOOGLE_TOKEN_ENDPOINT == oauth.TOKEN_ENDPOINT


def test_the_production_endpoints_are_https():
    """Both carry a credential: the refresh token to the token endpoint, the
    access token to the list. Neither is a knob - they are constants - and this
    is what stops one quietly becoming plain http in an edit."""
    assert feeder.GOOGLE_TOKEN_ENDPOINT.startswith("https://")
    assert feeder.GOOGLE_HEALTH_LIST_URL.startswith("https://")


def test_an_empty_history_posts_the_unavailable_gauge_through_a_whole_cycle(tmp_path):
    """The end of the empty-history story, asserted where it matters.

    200 with no points must reach the panel as value 0 with NO `observed_at` -
    the one number in this file nobody measured, and the only body allowed to
    carry it - and the failure line must say the account has logged nothing
    rather than implying a broken source.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = dict(google_env(tmp_path),
               _identity="u", _feed_url="http://127.0.0.1:1/api/feed",
               WEIGHT_STATE_FILE=str(state_dir / "weight-state.json"),
               STATE_DIRECTORY=str(state_dir))
    sent = []
    poster = lambda body, *a: (sent.append(body), (True, "HTTP 200"))[1]
    with loopback_server(fake_google(a_body())) as (base, _seen):
        reader = lambda e: feeder.read_google_health(
            e, list_url=base + "/points", token_endpoint=base + "/token")
        posted, failures, _ = feeder.run_cycle(
            env, now=NOW, readers={"google-health": reader}, poster=poster,
            goal_loader=lambda d, *a: (GOAL, "health.md"))
    assert posted[0][1] is False
    assert sent[-1]["value"] == 0.0
    assert "observed_at" not in sent[-1]
    assert feeder.is_fresh(sent[-1], NOW) is False
    assert len(failures) == 1
    assert "no weight logged" in failures[0]


def test_a_real_reading_reaches_the_panel_at_the_instant_it_was_true(tmp_path):
    """The other end of the same cycle: a live read posts the weight at the
    sample's own stamp, never at `now`, and NagLight sees it as fresh."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = dict(google_env(tmp_path),
               _identity="u", _feed_url="http://127.0.0.1:1/api/feed",
               WEIGHT_STATE_FILE=str(state_dir / "weight-state.json"),
               STATE_DIRECTORY=str(state_dir))
    sent = []
    poster = lambda body, *a: (sent.append(body), (True, "HTTP 200"))[1]
    with loopback_server(fake_google(CAPTURED_SHAPE)) as (base, _seen):
        reader = lambda e: feeder.read_google_health(
            e, list_url=base + "/points", token_endpoint=base + "/token")
        posted, failures, _ = feeder.run_cycle(
            env, now=NOW, readers={"google-health": reader}, poster=poster,
            goal_loader=lambda d, *a: (GOAL, "health.md"))
    assert failures == []
    assert posted[0][1] is True
    assert round(sent[-1]["value"], 1) == 176.0
    assert sent[-1]["observed_at"] == feeder.iso8601_utc(CAPTURED_AT)
    assert sent[-1]["observed_at"] != feeder.iso8601_utc(NOW)
    assert feeder.is_fresh(sent[-1], NOW) is True
    # The state file holds a weight and a timestamp and NOTHING ELSE - no
    # token, no header, no response body, no user id.
    stored = json.loads((state_dir / "weight-state.json").read_text())
    assert sorted(stored["weight"]) == ["observed_at", "value"]
    assert SENTINEL_ID not in json.dumps(stored)
    assert "REFRESH-TOKEN-ABC" not in json.dumps(stored)


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
    with feeder.open_for_write(state, state, str(tmp_path)) as handle:
        handle.write("{}")
    with feeder.open_for_write(state + ".tmp", state, str(tmp_path)) as handle:
        handle.write("{}")
    for forbidden in (str(tmp_path / "other.json"),
                      str(tmp_path / "google-health-token.json"),
                      str(tmp_path / "sub" / "weight-state.json"),
                      str(tmp_path / ".netrc")):
        with pytest.raises(PermissionError):
            feeder.open_for_write(forbidden, state, str(tmp_path))


def test_a_symlinked_temp_file_cannot_truncate_the_refresh_token_sr022(tmp_path):
    """THE CROSS-REVIEW DEFECT, against the real filesystem.

    `os.path.abspath` does not resolve symlinks, so a link at `<state>.tmp`
    pointing at the Owner's Google refresh token passed the old allow-list —
    the STRING matched — and `save_state` opened it "w" and truncated it. A
    refresh token minted at a browser does not survive that; it costs a person
    a trip back to a browser.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    token = tmp_path / "google-health-token.json"
    token.write_text('{"refresh_token":"REAL-REFRESH-TOKEN"}', encoding="utf-8")
    state = state_dir / "weight-state.json"
    try:
        os.symlink(str(token), str(state) + ".tmp")
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    with pytest.raises(PermissionError):
        feeder.save_state({"weight": {"value": 191.4, "observed_at": NOW}},
                          str(state), str(state_dir))
    assert token.read_text(encoding="utf-8") == '{"refresh_token":"REAL-REFRESH-TOKEN"}'


def test_a_temp_file_planted_between_the_check_and_the_open_is_refused_sr022(tmp_path):
    """The check-then-open race, not just the check.

    `open_no_follow` unlinks first — which destroys a planted LINK and never
    the file it points at — then creates with O_CREAT|O_EXCL, so the kernel
    refuses anything that appeared in the gap rather than trusting a verdict
    taken a moment earlier.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    victim = tmp_path / "refresh-token"
    victim.write_text("KEEP-ME", encoding="utf-8")
    tmp_file = state_dir / "weight-state.json.tmp"
    try:
        os.symlink(str(victim), str(tmp_file))
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")
    with feeder.open_no_follow(str(tmp_file), root=str(state_dir)) as handle:
        handle.write("{}")
    assert victim.read_text(encoding="utf-8") == "KEEP-ME"
    assert not tmp_file.is_symlink()


def test_an_intermediate_directory_swapped_after_the_verdict_is_refused_sr022(tmp_path):
    """THE CROSS-REVIEW DEFECT: O_NOFOLLOW protects the LAST hop only.

    The window is real and the timing is not exotic: `token_write_verdict` (or
    `writable_path_verdict`) resolves the whole path, and then the open walks
    it again. Replace `.../tokens` with a link to somewhere else in between and
    every guard above has already passed - the old open followed the swapped
    directory and put the file outside the state directory the module says it
    cannot leave.

    THIS TEST PLANTS THE LINK IN THAT WINDOW BY CALLING THE OPEN DIRECTLY, on
    the real filesystem, which is exactly the state the process would be in a
    microsecond after the verdict. No mock: the verdict already returned, and
    what happens next is the only thing under test.
    """
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = root / "tokens" / "weight-state.json.tmp"
    # ...the verdict has now resolved cleanly. The swap:
    (root / "tokens").rmdir()
    try:
        os.symlink(str(outside), str(root / "tokens"), target_is_directory=True)
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    with pytest.raises(OSError):
        feeder.open_no_follow(str(target), root=str(root))
    assert not (outside / "weight-state.json.tmp").exists(), (
        "the write landed outside the state directory")


def test_the_contained_open_still_writes_through_real_directories_sr022(tmp_path):
    """The other half: a guard that refuses everything is not a guard.

    A genuine nested directory under the root must still be walked and written,
    or the feeder simply stops working on the hub - where the state file sits
    one level down from StateDirectory= in exactly this shape.
    """
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    target = root / "tokens" / "weight-state.json.tmp"
    with feeder.open_no_follow(str(target), root=str(root)) as handle:
        handle.write("{}")
    assert target.read_text(encoding="utf-8") == "{}"
    assert feeder.path_components_under(str(target), str(root)) == \
        ["tokens", "weight-state.json.tmp"]


def test_a_path_that_is_not_under_the_root_is_never_opened_sr022(tmp_path):
    """The walk starts AT the root, so a path it cannot reach from there is
    refused rather than opened by its name. This is the case the containment
    claim cannot be made for, so it is not made."""
    root = tmp_path / "state"
    root.mkdir()
    stray = tmp_path / "elsewhere.json"
    with pytest.raises(PermissionError) as err:
        feeder.open_no_follow(str(stray), root=str(root))
    assert "cannot be contained" in str(err.value)
    assert not stray.exists()


def test_open_for_write_hands_the_state_root_to_the_open_sr022(tmp_path, monkeypatch):
    """THE WIRING, and it is named as such.

    The containment above is proved against `open_no_follow` on the real
    filesystem; what this asserts is only that the ONE caller passes its root
    in, because a perfect guard nobody calls with a root is the same as no
    guard. It is a weaker test than the one above and is not a substitute for
    it - the previous three rounds each lost a first-pass mutation to a test
    that leaned on a neighbour, so which check carries what is written down.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "weight-state.json"
    seen = {}

    def recorder(path, root=None):
        seen["path"], seen["root"] = path, root
        return io.StringIO()

    monkeypatch.setattr(feeder, "open_no_follow", recorder)
    feeder.open_for_write(str(state_path) + ".tmp", str(state_path), str(state_dir))
    assert seen["root"] == str(state_dir)


def test_the_state_file_must_sit_inside_the_services_own_state_directory_sr022(tmp_path):
    """The allow-list alone only proves the feeder wrote where the .env told
    it. A knob pointing at the token is refused because the token is not under
    StateDirectory=, whatever the knob says."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "home" / ".config" / "weight-state.json"
    outside.parent.mkdir(parents=True)
    reason = feeder.writable_path_verdict(str(outside), str(outside), str(state_dir))
    assert reason and "state directory" in reason
    with pytest.raises(PermissionError):
        feeder.open_for_write(str(outside), str(outside), str(state_dir))
    assert not outside.exists()


def test_the_state_root_comes_from_the_units_state_directory_sr022():
    assert feeder.resolve_state_root({"STATE_DIRECTORY": "/var/lib/homehub-weight"}) \
        == "/var/lib/homehub-weight"
    assert feeder.resolve_state_root({}) == feeder.DEFAULT_STATE_ROOT
    assert feeder.resolve_state_root({"STATE_DIRECTORY": " "}) == feeder.DEFAULT_STATE_ROOT
    unit = (REPO / "stack" / "weight" / "homehub-weight.service").read_text(
        encoding="utf-8")
    # Directives matched at the START OF A LINE — the vacuous-substring shape
    # this build has now found repeatedly is not repeated here.
    directives = [l.strip() for l in unit.splitlines()
                  if l and not l.startswith(("#", "[", " "))]
    assert "StateDirectory=homehub-weight" in directives
    assert feeder.DEFAULT_STATE_ROOT == "/var/lib/homehub-weight"


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
           "STATE_DIRECTORY": str(state_dir),
           "WEIGHT_TOKEN_FILE": str(token_file)}
    feeder.run_cycle(env, now=NOW,
                     readers={"google-health": lambda e: (191.4, NOW)},
                     poster=lambda *a: (True, "HTTP 200"),
                     goal_loader=lambda d, *a: (GOAL, "health.md"))

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


def test_the_item_location_knobs_are_declared_in_env_example_sr022():
    """The two LOCATION knobs must ship, or moving the item needs a code change.

    THE FieldSchema HALF IS OWED AND DELIBERATELY NOT ASSERTED HERE.
    `FieldSchema.psd1` lives in the HomeHub repo, which this worktree may not
    edit, so `WEIGHT_ITEM_CATEGORY` / `WEIGHT_ITEM_ID` are not in the emitter's
    field list yet. That is SAFE rather than broken: an undeclared knob is
    simply absent from the emitted .env, a blank/absent knob takes the default,
    and the default IS the shape the Owner's sheet already syncs. What it costs
    is that MOVING the item currently needs an .env edit on the hub rather than
    a deploy-config change. Recorded in docs/status.md as owed to HomeHub; do
    not "fix" it by adding the knobs to the list above, which would turn
    `test_the_knobs_are_declared_where_deploy_reads_them` red for a file this
    block cannot touch.
    """
    env_example = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    for knob in ("WEIGHT_ITEM_CATEGORY", "WEIGHT_ITEM_ID"):
        assert "\n%s=" % knob in env_example, "%s is not in .env.example" % knob
    # And the shipped defaults must be the ones the code falls back to, or the
    # two would drift and nobody would notice until the item moved.
    assert "\nWEIGHT_ITEM_CATEGORY=%s\n" % feeder.DEFAULT_GOAL_CATEGORY in env_example
    assert "\nWEIGHT_ITEM_ID=%s\n" % feeder.DEFAULT_GOAL_ITEM_ID in env_example


def test_no_goal_shaped_knob_is_declared_anywhere_deploy_reads_sr022():
    """The goal must not merely be unused on the hub — it must be ABSENT.

    Written after the mutation run, which added `WEIGHT_GOAL_LB=180` to
    .env.example and left the whole suite green: the runtime refusal was
    asserted, but nothing stopped a goal knob from being DECLARED. A declared
    knob is a second home for the household's intent whatever the code does
    with it — someone fills it in, nothing reads it, and the person cannot work
    out why the bar will not move. `WEIGHT_DEFINITIONS_DIR` is exempt because
    it names the directory, never the number.

    STILL MEANS SOMETHING AFTER THE 2026-09-09 CHANGE, AND THAT WAS CHECKED.
    The goal moved from a top-level frontmatter key to an item's `target`, and
    the two knobs that move with it name the item's LOCATION - category and id.
    They are deliberately NOT called `WEIGHT_GOAL_*`, so this scan is not
    quietly satisfied by a rename: it still fails on any name that could carry
    a number, and `test_no_hub_knob_can_supply_the_goal_sr022` sets the two
    location knobs to `170` and proves that still yields no goal.
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


# ═══════════════════════════════════════════════════════════════════════════
# V2 — the POST carries the Owner's body weight, so it may not leave this box
# ═══════════════════════════════════════════════════════════════════════════

def _post_env(**extra):
    env = {"_identity": "108000000000000000001",
           "WEIGHT_FEED_TOKEN": "FEED-TOKEN-SECRET"}
    env.update(extra)
    return env


def test_the_post_refuses_a_redirect_and_never_re_sends_the_body_sr022():
    """urllib's default opener follows redirects and copies the request's
    headers onto the new request, so the validated loopback endpoint could
    answer `302 Location: <anywhere>` and urllib would re-send the POST — feed
    token, identity header, and a body whose one number is the Owner's body
    weight. Both halves are asserted: the post fails, and the second server
    records that nothing ever arrived."""
    with loopback_server(plain_200) as (elsewhere, arrived):
        with loopback_server(redirect_to(elsewhere + "/api/feed")) as (front, front_seen):
            ok, detail = feeder.post_gauge(
                {"kind": "gauge", "id": "weight", "value": 191.4, "target": GOAL},
                front + "/api/feed", _post_env(), 10)
        assert ok is False
        assert "redirect" in detail
        assert arrived == [], "the body weight was re-sent to the redirect target"
    assert front_seen and b"191.4" in front_seen[0]["body"]
    assert "FEED-TOKEN-SECRET" not in detail


def test_the_post_ignores_an_http_proxy_in_the_environment_sr022(monkeypatch):
    """An `http_proxy` exported into the unit would route the POST — token,
    identity header and body weight — through a LAN proxy that then sees all of
    it, without the feed URL changing at all."""
    with loopback_server(plain_200) as (proxy_url, proxy_seen):
        with loopback_server(plain_200) as (target, target_seen):
            for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(name, proxy_url)
            ok, detail = feeder.post_gauge(
                {"kind": "gauge", "id": "weight", "value": 191.4, "target": GOAL},
                target + "/api/feed", _post_env(), 10)
    assert ok is True, detail
    assert proxy_seen == [], "a body weight went through the environment's proxy"
    assert target_seen and b"191.4" in target_seen[0]["body"]
    assert target_seen[0]["headers"]["X-Forwarded-User"] == "108000000000000000001"


def test_the_post_re_validates_the_address_it_actually_reached_sr022():
    """The peer check runs inside connect(), after the handshake and BEFORE a
    byte of the request line is written — so a connection that lands somewhere
    it should not is dropped with the token and the weight still unsent."""
    import urllib.request as urlreq
    with loopback_server(plain_200) as (target, seen):
        original = feeder.is_local_destination
        try:
            feeder.is_local_destination = lambda host, bridges=None: False
            request = urlreq.Request(target + "/api/feed", data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
            with pytest.raises(feeder.EgressRefused):
                feeder.feed_opener(bridge_addresses=[]).open(request, timeout=10)
        finally:
            feeder.is_local_destination = original
    assert seen == [], "bytes reached a peer the guard should have refused"


def test_the_vendor_opener_the_blocked_half_must_use_refuses_a_redirect_sr022():
    """The door the Google Health reader will use, built before the reader.

    The scope this feeder will one day hold grants blood glucose, body fat,
    oxygen saturation, core temperature and heart-rate metrics as well as
    weight — there is no weight-only scope — so a 302 carrying `Authorization`
    to another host hands out all of it.
    """
    import urllib.request as urlreq
    with loopback_server(plain_200) as (elsewhere, arrived):
        with loopback_server(redirect_to(elsewhere + "/v4/users/me")) as (vendor, _):
            request = urlreq.Request(
                vendor + "/v4/users/me",
                headers={"Authorization": "Bearer HEALTH-TOKEN-SECRET"})
            with pytest.raises(feeder.EgressRefused):
                feeder.vendor_opener().open(request, timeout=10)
        assert arrived == [], "a health-data token followed a redirect"


def test_the_vendor_opener_inherits_no_proxy_from_the_environment_sr022(monkeypatch):
    """Deliberate, and different from the feed only in what it CANNOT check: a
    Google call is outbound by design, so there is no peer check — but a
    box-wide proxy would still terminate TLS in front of a health-data token,
    and there is no knob to opt back in."""
    import urllib.request as urlreq
    with loopback_server(plain_200) as (proxy_url, proxy_seen):
        with loopback_server(plain_200) as (vendor, vendor_seen):
            for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(name, proxy_url)
            request = urlreq.Request(
                vendor + "/v4/users/me",
                headers={"Authorization": "Bearer HEALTH-TOKEN-SECRET"})
            feeder.vendor_opener().open(request, timeout=10).read()
    assert proxy_seen == [], "a health-data token went through a LAN proxy"
    assert vendor_seen and vendor_seen[0]["path"] == "/v4/users/me"


def test_a_remote_error_body_is_never_written_to_the_journal_sr022():
    """`exc.read()[:200]` put whatever a responding server or an interposed
    proxy chose to reflect — a token echoed back included — into a string
    `main` prints to stderr and systemd persists in the journal."""
    def reflect(handler):
        body = json.dumps({"error": "Authorization: Bearer FEED-TOKEN-SECRET"}).encode()
        handler.send_response(500)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with loopback_server(reflect) as (target, _):
        ok, detail = feeder.post_gauge(
            {"kind": "gauge", "id": "weight", "value": 191.4, "target": GOAL},
            target + "/api/feed", _post_env(), 10)
    assert ok is False
    assert "500" in detail
    assert "FEED-TOKEN-SECRET" not in detail and "Authorization" not in detail


def test_an_unexpected_reader_exception_records_its_type_and_not_its_message(tmp_path):
    """B7 logs only the exception TYPE, and this feeder now copies that.

    The broad `except Exception` here is the branch the real OAuth reader will
    fall into, and an exception raised inside urllib carries the request
    object: `str(exc)` on one of those prints the Authorization header straight
    into the journal. Asserted forward-looking, with an exception carrying a
    header in its message the way a urllib one would.
    """
    def leaky(env):
        raise RuntimeError(
            "<urlopen error> while requesting Request(headers={'Authorization': "
            "'Bearer HEALTH-TOKEN-SECRET'})")

    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(tmp_path / "weight-state.json"),
           "STATE_DIRECTORY": str(tmp_path)}
    posted, failures, _ = feeder.run_cycle(
        env, now=NOW, readers={"google-health": leaky},
        poster=lambda *a: (True, "HTTP 200"),
        goal_loader=lambda d, *a: (GOAL, "health.md"))

    joined = " ".join(failures)
    assert "RuntimeError" in joined, "the failure must still be named"
    assert "HEALTH-TOKEN-SECRET" not in joined
    assert "Authorization" not in joined
    assert posted[0][1] is False, "and the gauge is unavailable, never fresh"


# ═══════════════════════════════════════════════════════════════════════════
# Loaded state is input, not memory
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("entry,why", [
    (None, "not an object at all"),
    ("191.4", "a string"),
    ({"value": -500.0, "observed_at": NOW - 3600}, "a negative body weight"),
    ({"value": 100000.0, "observed_at": NOW - 3600}, "far outside any human range"),
    ({"value": 0.0, "observed_at": NOW - 3600}, "the sentinel wearing a stamp"),
    ({"value": float("nan"), "observed_at": NOW - 3600}, "not a number"),
    ({"value": True, "observed_at": NOW - 3600}, "a bool, which Python calls an int"),
    ({"value": "191.4", "observed_at": NOW - 3600}, "a numeric string"),
    ({"value": 191.4, "observed_at": NOW + 90000}, "a stamp in the FUTURE"),
    ({"value": 191.4, "observed_at": 1}, "a stamp before this feeder existed"),
    ({"value": 191.4, "observed_at": "yesterday"}, "a stamp that is not a number"),
    ({"value": 191.4}, "no stamp at all"),
])
def test_a_nonsensical_stored_reading_is_a_failure_not_history_sr022(entry, why):
    """Corrupt or tampered state used to post a fabricated reading: nothing
    checked it, so `-500 lb` went on the wall and a future stamp made a source
    dead for a week render live. A stored reading that cannot be true is not
    history — it gets the answer a source that never succeeded gets."""
    assert feeder.validate_stored_reading(entry, NOW) is None, why


def test_a_credible_stored_reading_is_still_kept_sr022():
    kept = feeder.validate_stored_reading(
        {"value": 191.4, "observed_at": NOW - 3 * 24 * 3600}, NOW)
    assert kept == {"value": 191.4, "observed_at": NOW - 3 * 24 * 3600}


def test_the_state_file_is_filtered_AT_THE_LOAD_sr022(tmp_path):
    """One of the two layers, on its own — found by the mutation run, which
    showed that `load_state` and `build_post` each hid the other's absence."""
    state = tmp_path / "weight-state.json"
    state.write_text(json.dumps({"weight": {"value": -500.0,
                                            "observed_at": NOW - 3600}}),
                     encoding="utf-8")
    assert feeder.load_state(str(state), NOW) == {}
    state.write_text(json.dumps({"weight": {"value": 191.4,
                                            "observed_at": NOW - 3600}}),
                     encoding="utf-8")
    assert feeder.load_state(str(state), NOW) == {
        "weight": {"value": 191.4, "observed_at": NOW - 3600}}


def test_a_corrupt_stored_reading_is_refused_AT_THE_POINT_OF_USE_sr022():
    """The other layer, handed the entry directly."""
    body, fresh = feeder.build_post(
        None, {"value": -500.0, "observed_at": NOW - 3600}, GOAL, NOW)
    assert fresh is False
    assert body["value"] == 0.0 and "observed_at" not in body
    good, fresh = feeder.build_post(
        None, {"value": 191.4, "observed_at": NOW - 3600}, GOAL, NOW)
    assert fresh is False and good["value"] == 191.4


def test_corrupt_state_posts_unavailable_and_never_a_fabricated_weight_sr022(tmp_path):
    """End to end, through the real state file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = state_dir / "weight-state.json"
    state.write_text(json.dumps({"weight": {"value": -500.0,
                                            "observed_at": NOW + 99999}}),
                     encoding="utf-8")
    sent = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(state), "STATE_DIRECTORY": str(state_dir)}

    def broken(e):
        raise feeder.SourceFailure("google-health: HTTP 401")

    posted, failures, _ = feeder.run_cycle(
        env, now=NOW, readers={"google-health": broken},
        poster=lambda body, *a: (sent.append(body), (True, "HTTP 200"))[1],
        goal_loader=lambda d, *a: (GOAL, "health.md"))

    assert sent[0]["value"] == 0.0
    assert "observed_at" not in sent[0]
    assert feeder.is_fresh(sent[0], NOW) is False
    assert posted[0][1] is False


def test_the_invariant_holds_across_every_fix_sr022(tmp_path):
    """the body carries a stamp from this cycle IF AND ONLY IF it read the
    source — restated whole, because six changes in this round could each have
    broken it from a different direction."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(state_dir / "weight-state.json"),
           "STATE_DIRECTORY": str(state_dir)}
    sent = []
    poster = lambda body, *a: (sent.append(body), (True, "HTTP 200"))[1]
    loader = lambda d, *a: (GOAL, "health.md")

    measured_at = NOW - 3600
    posted, _, _ = feeder.run_cycle(
        env, now=NOW, readers={"google-health": lambda e: (191.4, measured_at)},
        poster=poster, goal_loader=loader)
    assert posted[0][1] is True
    assert sent[-1]["observed_at"] == feeder.iso8601_utc(measured_at)

    def broken(e):
        raise feeder.SourceFailure("google-health: HTTP 401")

    later = NOW + 8 * 24 * 3600      # past the static 7-day horizon
    posted, _, _ = feeder.run_cycle(env, now=later, readers={"google-health": broken},
                                    poster=poster, goal_loader=loader)
    assert posted[0][1] is False
    assert sent[-1]["value"] == 191.4
    assert sent[-1]["observed_at"] == feeder.iso8601_utc(measured_at)
    assert feeder.is_fresh(sent[-1], later) is False


def test_no_path_through_the_new_parser_can_fabricate_a_reading_sr022():
    """WHAT THE ABSENCE ASSERTION USED TO PROTECT, NOW PROTECTED DIRECTLY.

    Until 2026-09-09 this asserted that no parser existed at all, because none
    had ever seen a real body. The parser exists now, so the property it stood
    for is asserted where it actually lives: EVERY way the vendor half can fail
    ends in a SourceFailure and therefore in the unavailable gauge, and NONE of
    them returns a number. `read_google_health` returns a reading if and only if
    a real body point carried one.
    """
    for env in ({"_now": NOW},                       # no token file configured
                {"_now": NOW, "WEIGHT_TOKEN_FILE": "/nonexistent/token.json"}):
        with pytest.raises(feeder.SourceFailure):
            feeder.read_google_health(env)
    for body in ({}, {"dataPoints": []}, {"dataPoints": [{}]},
                 {"dataPoints": "weights"}, [], None,
                 {"dataPoints": [{"weight": {"weightGrams": 0}}]}):
        with pytest.raises(feeder.SourceFailure):
            feeder.parse_weight_datapoint(body, NOW)
    # ...and every one of those is caught by `run_cycle` as the unavailable
    # gauge rather than as a crash, which is the property that matters on a
    # wall: value 0, no stamp, "unavailable", never a plausible wrong number.
    body, fresh = feeder.build_post(None, None, GOAL, NOW)
    assert fresh is False and body["value"] == 0.0
    assert "observed_at" not in body


# ===========================================================================
# B11 — SCOPED DEFINITIONS READ ACCESS (the deployment blocker, 2026-09-09)
# ===========================================================================
# THE BLOCKER, measured on the hub with `systemd-run` as the real service
# account, and NOT re-derived here:
#
#   * the definitions directory is `drwx------ hub hub`, and its ancestors are
#     root-only (`/var/lib/docker` is `drwx--x---`, nothing for "other"), so
#     `BindReadOnlyPaths=<dir>` at the SAME path leaves the account unable to
#     traverse to it: NOT-READABLE;
#   * binding that directory to a target the account owns is ALSO
#     NOT-READABLE - the 0700 directory is the blocker, not the target;
#   * binding the single category FILE (0644) to a target inside the account's
#     StateDirectory is READABLE, and `touch` on it is denied.
#
# `setfacl` is not installed and an ACL would not survive anyway: NagLight
# applies definitions by MkdirTemp + populate + rename-into-place, so the
# `definitions` inode is replaced wholesale on every sync.
#
# SCOPE IS THE REASON, not merely the workaround. The volume holds EVERY
# household member's tracker data; a single-user gauge must not hand this
# service account read access to all of it. These tests assert the shape that
# is both the safe one and the one that works.

SETUP_WEIGHT = REPO / "stack" / "weight" / "setup-weight.sh"
UNIT_PATH = REPO / "stack" / "weight" / "homehub-weight.service"
MOUNT_DIR = "/var/lib/homehub-weight/definitions"

_HAVE_BASH = bool(os.environ.get("SHELL")) or Path(
    "C:/Program Files/Git/bin/bash.exe").exists()
needs_bash = pytest.mark.skipif(not _HAVE_BASH, reason="needs bash")

HEALTH_DEFS = (
    "---\n"
    "category: Health\n"
    "color_weight: 1.5\n"
    "items:\n"
    "  - id: weigh-in\n"
    "    title: Step on the scale\n"
    "    target: 170\n"
    "    unit: lb\n"
    "---\n"
    "\n"
    "Free notes live down here.\n"
)


def _emit_dropin(tmp_path, files, category=None, account=None):
    """Run the REAL setup-weight.sh drop-in generator over a definitions tree.

    Returns (CompletedProcess, dropin_dir). Nothing is grepped out of the
    script itself: what is asserted is what the script WROTE, which is the V2
    lesson applied to the bind as well as to the account.
    """
    import subprocess

    defs_dir = tmp_path / "definitions"
    defs_dir.mkdir(exist_ok=True)
    for name, body in files.items():
        (defs_dir / name).write_text(body, encoding="utf-8")
    out = tmp_path / "dropin"
    env = dict(
        os.environ,
        WEIGHT_ENV_FILE="/nonexistent",     # never read the developer's .env
        WEIGHT_ENABLED="true",
        WEIGHT_USER="sub-not-a-real-google-id",
        WEIGHT_FEED_URL="http://127.0.0.1:8099/api/v1/gauges",
        WEIGHT_DEFINITIONS_DIR=str(defs_dir),
        WEIGHT_USER_ACCOUNT=account or "homehub-weight",
    )
    if category is not None:
        env["WEIGHT_ITEM_CATEGORY"] = category
    proc = subprocess.run(
        ["bash", str(SETUP_WEIGHT), "--emit-dropin", str(out)],
        env=env, capture_output=True, text=True)
    return proc, out


def _bind_lines(dropin_dir):
    conf = (dropin_dir / "10-account.conf").read_text(encoding="utf-8")
    return [l for l in conf.splitlines() if l.startswith("BindReadOnlyPaths=")]


def _one_bind(dropin_dir):
    """The (source, target) of the single bind, refusing to guess if there are
    two - a test that quietly took the first would be the same defect it is
    here to catch."""
    lines = _bind_lines(dropin_dir)
    assert len(lines) == 1, "expected exactly one bind, got %r" % (lines,)
    spec = lines[0].split("=", 1)[1]
    assert spec.startswith("-"), (
        "the bind must carry systemd's `-` prefix: without it a category file "
        "that has been renamed away fails the unit at 226/NAMESPACE every "
        "fifteen minutes. Got %r" % spec)
    source, target = spec[1:].rsplit(":", 1)
    return source, target


class TestScopedDefinitionsBind:
    """The drop-in exposes ONE definitions file, resolved by reading it."""

    @needs_bash
    def test_the_file_is_found_by_frontmatter_not_by_its_name_sr022(self, tmp_path):
        """THE DEFECT THIS FORBIDS: deriving `health.md` from `Health`.

        The filename is NagLight's slug rule, which lives in another repo and
        can change without telling us; the `category:` in the frontmatter is
        the thing the feeder itself matches on. So the tree here is booby
        trapped both ways round - the file that CARRIES category `Health` is
        called `tracker-2b.md`, and there IS a `health.md`, declaring something
        else entirely. A script that lowercased the category would bind the
        wrong member's tracker and say nothing about it.
        """
        proc, out = _emit_dropin(tmp_path, {
            "tracker-2b.md": HEALTH_DEFS,
            "health.md": "---\ncategory: Chores\nitems:\n  - id: bins\n---\n",
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        source, target = _one_bind(out)
        assert Path(source).name == "tracker-2b.md", source
        assert Path(source).name != "health.md"
        assert target == MOUNT_DIR + "/tracker-2b.md", target

    @needs_bash
    def test_the_resolved_file_is_the_one_the_feeder_then_reads_sr022(self, tmp_path):
        """AN ORACLE, not a restatement. The shell resolves a file; the FEEDER
        is then pointed at a directory holding only that file and must find the
        goal in it. If the two ever disagree about what `category:` means -
        trimming, case, quoting, a `# comment`, depth - this fails, and no
        amount of agreement between the shell and its own test would save it.
        """
        proc, out = _emit_dropin(tmp_path, {
            "tracker-2b.md": HEALTH_DEFS,
            "zz-other-member.md": "---\ncategory: Chores\nitems:\n  - id: bins\n---\n",
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        source, target = _one_bind(out)
        # What the SERVICE sees: a directory holding only the bound file, under
        # the name the bind gives it.
        mounted = tmp_path / "mounted"
        mounted.mkdir()
        (mounted / Path(target).name).write_text(
            Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        goal, where = feeder.load_goal_from_definitions(str(mounted))
        assert goal == DEFS_GOAL
        assert Path(where).name == Path(target).name

    @needs_bash
    def test_two_files_declaring_the_category_are_refused_sr022(self, tmp_path):
        """REFUSED, NEVER PICKED. Two files under one category means the goal
        could be in either, and binding one would silently follow directory
        order - the same refusal the feeder makes when two files hold the item.
        """
        proc, out = _emit_dropin(tmp_path, {
            "a-tracker.md": HEALTH_DEFS,
            "b-tracker.md": HEALTH_DEFS,
        })
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "REFUSED" in proc.stdout
        # BOTH are named: "one of your files is ambiguous" is not actionable.
        assert "a-tracker.md" in proc.stdout and "b-tracker.md" in proc.stdout
        # And nothing was written, so a refusal cannot leave a half-made bind.
        assert not (out / "10-account.conf").exists()

    @needs_bash
    def test_one_file_declaring_the_category_twice_is_refused_sr022(self, tmp_path):
        """Same rule inside one file: which category the file IS is not
        guessable, and weight_feeder.one_value refuses it too."""
        proc, out = _emit_dropin(tmp_path, {
            "tracker.md": "---\ncategory: Health\ncategory: Chores\n---\n",
        })
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "tracker.md" in proc.stdout

    @needs_bash
    def test_no_matching_file_binds_nothing_and_is_not_a_failure_sr022(self, tmp_path):
        """A NOT-YET-SYNCED TRACKER MUST NOT WEDGE PROVISIONING. A renamed
        category or a tracker that has not synced yet is a normal state at
        firstboot: the drop-in is written with NO bind, the feeder sees an
        empty definitions directory and takes its existing "no goal, nothing
        posted" path, and setup exits 0 so firstboot carries on.
        """
        proc, out = _emit_dropin(tmp_path, {
            "zz-other-member.md": "---\ncategory: Chores\nitems:\n  - id: bins\n---\n",
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _bind_lines(out) == []
        assert "WARNING" in proc.stdout, (
            "a box that binds nothing must SAY so; a silent no-op is how a "
            "dark gauge becomes an unexplained one")
        # The override still ships, so the service looks at the (empty) mount
        # target rather than at a host path it cannot traverse to.
        env = (out / "20-definitions.env").read_text(encoding="utf-8")
        assert "WEIGHT_DEFINITIONS_DIR=%s\n" % MOUNT_DIR in env

    @needs_bash
    def test_the_missing_source_is_tolerated_by_the_dash_prefix_sr022(self, tmp_path):
        """The `-` is asserted on the line itself, because the failure it
        prevents is invisible until the hub: measured on systemd 255, a bind
        with no `-` and a missing source fails the unit at 226/NAMESPACE before
        the feeder ever runs, and the timer repeats that every fifteen minutes
        with no diagnosis. `_one_bind` refuses a spec without it."""
        proc, out = _emit_dropin(tmp_path, {"tracker-2b.md": HEALTH_DEFS})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _bind_lines(out)[0].startswith("BindReadOnlyPaths=-")

    @needs_bash
    def test_only_the_one_file_is_exposed_not_the_household_sr022(self, tmp_path):
        """SCOPE IS THE POINT. The definitions directory is one member's
        subtree of a volume holding every member's tracker data. Exactly one
        bind, and the DIRECTORY must appear nowhere in the drop-in - binding it
        would hand this account read access to all of it (and would not work
        anyway: it is 0700 under root-only ancestors).
        """
        defs_files = {
            "tracker-2b.md": HEALTH_DEFS,
            "m2-chores.md": "---\ncategory: Chores\nitems:\n  - id: bins\n---\n",
            "m3-reading.md": "---\ncategory: Reading\nitems:\n  - id: pages\n---\n",
        }
        proc, out = _emit_dropin(tmp_path, defs_files)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        source, _target = _one_bind(out)
        conf = (out / "10-account.conf").read_text(encoding="utf-8")
        defs_dir = str(tmp_path / "definitions")
        for line in conf.splitlines():
            if not line.startswith("BindReadOnlyPaths="):
                continue
            spec = line.split("=", 1)[1].lstrip("-")
            assert spec.rsplit(":", 1)[0] != defs_dir, (
                "the whole definitions directory is bound: %r" % line)
        for name in ("m2-chores.md", "m3-reading.md"):
            assert name not in conf, (
                "%s is another household member's tracker and must not be "
                "reachable by this account" % name)
        assert Path(source).name == "tracker-2b.md"

    @needs_bash
    def test_a_nested_category_does_not_choose_the_file_sr022(self, tmp_path):
        """DEPTH IS THE STRUCTURE, in the shell reader as in the python one. A
        `category:` indented under `items:` is a field of an ITEM; reading it
        as the file's category is the depth-blind defect the feeder's own
        reader was rewritten to stop making, and it would bind a file that
        declares something else entirely."""
        proc, out = _emit_dropin(tmp_path, {
            "tracker.md": ("---\ncategory: Chores\nitems:\n"
                           "  - id: q\n    category: Health\n---\n"),
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _bind_lines(out) == []

    @needs_bash
    def test_a_prose_category_does_not_choose_the_file_sr022(self, tmp_path):
        """Only the frontmatter counts - the lines between the first two `---`
        fences. A `category:` in the free notes below is a person thinking out
        loud, which is exactly the line internal/defs draws."""
        proc, out = _emit_dropin(tmp_path, {
            "tracker.md": "---\ncategory: Chores\n---\n\ncategory: Health\n",
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _bind_lines(out) == []

    @needs_bash
    def test_the_category_match_is_trimmed_and_case_insensitive_sr022(self, tmp_path):
        """Both halves are typed by a person into a spreadsheet cell, and the
        feeder matches them trimmed and case-folded. If this script were
        stricter it would bind nothing where the feeder would have found the
        goal, and the panel would go dark for a difference in capitals."""
        proc, out = _emit_dropin(
            tmp_path, {"tracker.md": '---\ncategory:   "health"   \n---\n'},
            category="  HEALTH ")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        source, _ = _one_bind(out)
        assert Path(source).name == "tracker.md"


class TestDefinitionsPathOverride:
    """What the SERVICE sees for WEIGHT_DEFINITIONS_DIR, and how it wins."""

    @needs_bash
    def test_the_override_is_an_environment_file_not_an_environment_line_sr022(
            self, tmp_path):
        """MEASURED, NOT ASSUMED - and the obvious way round does not work.

        On systemd 255, `EnvironmentFile=` assignments are applied AFTER every
        `Environment=` assignment REGARDLESS OF ORDER. An
        `Environment=WEIGHT_DEFINITIONS_DIR=...` in this drop-in LOSES to the
        unit's `EnvironmentFile=/opt/homehub/stack/.env`, even though drop-ins
        are parsed later, and even with both lines in one file and the
        `Environment=` second (all four orders were run). Two
        `EnvironmentFile=` lines ARE applied in parse order with the last one
        winning, and drop-ins are parsed after the unit - so the override has
        to be a FILE, and this test fails if anyone "simplifies" it back.
        """
        proc, out = _emit_dropin(tmp_path, {"tracker-2b.md": HEALTH_DEFS})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        conf = (out / "10-account.conf").read_text(encoding="utf-8")
        assert "Environment=WEIGHT_DEFINITIONS_DIR" not in conf, (
            "an Environment= line here loses to the unit's EnvironmentFile=, "
            "measured on systemd 255 - the service would look at the host "
            "path it cannot traverse to")
        override = out / "20-definitions.env"
        pointed = [l.split("=", 1)[1] for l in conf.splitlines()
                   if l.startswith("EnvironmentFile=")]
        assert len(pointed) == 1, conf
        # Compared as a PATH, not as a string: bash writes back the directory
        # it was handed, so on Windows the separators come out mixed and a
        # string compare would pass or fail for the wrong reason.
        assert os.path.normcase(os.path.normpath(pointed[0])) == \
            os.path.normcase(os.path.normpath(str(override)))
        assert override.read_text(encoding="utf-8").strip().endswith(
            "WEIGHT_DEFINITIONS_DIR=%s" % MOUNT_DIR)

    @needs_bash
    def test_the_service_is_pointed_at_the_mount_target_not_the_host_path_sr022(
            self, tmp_path):
        """The host path in .env is where the file IS; the mount target is
        where the SERVICE can reach it. Pointing the service at the host path
        is the blocker this block exists to fix."""
        proc, out = _emit_dropin(tmp_path, {"tracker-2b.md": HEALTH_DEFS})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        _source, target = _one_bind(out)
        value = [l.split("=", 1)[1]
                 for l in (out / "20-definitions.env").read_text(
                     encoding="utf-8").splitlines()
                 if l.startswith("WEIGHT_DEFINITIONS_DIR=")]
        assert value == [MOUNT_DIR], value
        assert str(tmp_path) not in value[0], (
            "the service was pointed at the host definitions directory")
        # The bind must land INSIDE the directory the service is told to read.
        assert target.rsplit("/", 1)[0] == value[0]

    def test_the_mount_target_is_inside_the_units_state_directory_sr022(self):
        """A bound that the .env cannot move. The target must sit under the
        unit's own `StateDirectory=` - the same directory the credential write
        guard is bounded by - so "somewhere the account can reach" cannot drift
        into "somewhere a knob chose"."""
        unit = UNIT_PATH.read_text(encoding="utf-8")
        state = [l.split("=", 1)[1].strip() for l in unit.splitlines()
                 if l.startswith("StateDirectory=")]
        assert state, "the unit declares no StateDirectory="
        assert MOUNT_DIR.startswith("/var/lib/%s/" % state[0]), (
            "the mount target %s is not inside StateDirectory=%s"
            % (MOUNT_DIR, state[0]))
        script = SETUP_WEIGHT.read_text(encoding="utf-8")
        assert "WEIGHT_STATE_DIR=/var/lib/%s\n" % state[0] in script, (
            "setup-weight.sh and the unit disagree about the state directory")

    def test_the_setup_scripts_category_default_is_the_feeders_sr022(self):
        """A setup script resolving a DIFFERENT category from the one the
        feeder looks for would bind the wrong file and report success."""
        script = SETUP_WEIGHT.read_text(encoding="utf-8")
        assert ('WEIGHT_ITEM_CATEGORY="${WEIGHT_ITEM_CATEGORY:-%s}"'
                % feeder.DEFAULT_GOAL_CATEGORY) in script

    @needs_bash
    def test_the_account_is_still_written_from_the_knob_sr019(self, tmp_path):
        """Carried over, and re-asserted here because this block rewrote the
        drop-in generator: the account certified and the account systemd runs
        must still be one value."""
        proc, out = _emit_dropin(tmp_path, {"tracker-2b.md": HEALTH_DEFS},
                                 account="homehub-weight-alt")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        conf = (out / "10-account.conf").read_text(encoding="utf-8")
        assert "User=homehub-weight-alt" in conf
        assert "Group=homehub-weight-alt" in conf


class TestTheKnobsReachTheDropIn:
    """THE PATH PRODUCTION ACTUALLY USES, which the first pass of these tests
    did not touch at all.

    Written after a mutation survivor. Every test above hands the script its
    knobs through the PROCESS environment with `WEIGHT_ENV_FILE=/nonexistent`,
    which is convenient and is not how the hub runs it: firstboot calls
    `setup-weight.sh` with nothing exported, and the script reads
    `stack/.env` itself. Deleting `WEIGHT_ITEM_CATEGORY` from the list of keys
    it reads out of that file left the whole suite green while a household that
    had moved its goal to another category would silently get `Health` - the
    wrong member's file bound, or none, and a dark gauge with no explanation.
    """

    @needs_bash
    def test_the_category_and_the_path_are_read_from_the_env_file_sr022(
            self, tmp_path):
        import subprocess

        defs_dir = tmp_path / "definitions"
        defs_dir.mkdir()
        (defs_dir / "tracker-2b.md").write_text(HEALTH_DEFS, encoding="utf-8")
        (defs_dir / "wellness-notes.md").write_text(
            "---\ncategory: Wellness\nitems:\n  - id: weigh-in\n"
            "    target: 170\n    unit: lb\n---\n", encoding="utf-8")
        env_file = tmp_path / "dot.env"
        env_file.write_text(
            "WEIGHT_ENABLED=true\n"
            "WEIGHT_USER=sub-not-a-real-google-id\n"
            "WEIGHT_FEED_URL=http://127.0.0.1:8099/api/v1/gauges\n"
            "WEIGHT_DEFINITIONS_DIR=%s\n"
            "WEIGHT_ITEM_CATEGORY=Wellness\n" % defs_dir, encoding="utf-8")
        out = tmp_path / "dropin"
        # NOTHING is exported: every knob has to come out of the file, which is
        # exactly what firstboot does.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("WEIGHT_")}
        env["WEIGHT_ENV_FILE"] = str(env_file)
        proc = subprocess.run(
            ["bash", str(SETUP_WEIGHT), "--emit-dropin", str(out)],
            env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        source, _target = _one_bind(out)
        assert Path(source).name == "wellness-notes.md", (
            "WEIGHT_ITEM_CATEGORY in the .env was ignored, so the goal's "
            "category is whatever this script defaults to: %s" % proc.stdout)

    @needs_bash
    def test_emit_dropin_without_a_directory_refuses_sr022(self):
        """It writes files; being handed no path must be a refusal and not a
        pair of files somewhere nobody asked for."""
        import subprocess

        proc = subprocess.run(
            ["bash", str(SETUP_WEIGHT), "--emit-dropin"],
            env=dict(os.environ, WEIGHT_ENV_FILE="/nonexistent",
                     WEIGHT_ENABLED="true", WEIGHT_USER="sub",
                     WEIGHT_FEED_URL="http://127.0.0.1:8099/x",
                     WEIGHT_DEFINITIONS_DIR=str(REPO)),
            capture_output=True, text=True)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "REFUSED" in proc.stdout

    def test_the_install_path_hardens_the_mount_target_sr022(self):
        """READ AS A TEXT ASSERTION, AND HERE IS WHY IT IS ONE. These two lines
        run only as root on the hub, and neither this dev PC nor the pytest
        suite can exercise them; `--emit-dropin` deliberately stops before
        them. So this asserts the script's text, which is weaker than running
        it, and is written down as weaker rather than dressed up.

        What it holds:

        * the mount target is created **root:root 0755** inside the account's
          own StateDirectory. Not owned by the service account: the account
          must READ what is mounted there and must never be able to drop a file
          of its own beside it and have the feeder read that as the
          household's goal.
        * stale `*.md` are swept first. systemd CREATES a missing bind
          destination and LEAVES IT BEHIND as an empty file (measured on
          systemd 255), so a renamed category file leaves yesterday's stub
          sitting there for good.
        """
        script = SETUP_WEIGHT.read_text(encoding="utf-8")
        assert 'install -d -m 0755 -o root -g root "$WEIGHT_MOUNT_DIR"' in script, (
            "the mount target must be root-owned; an account-owned one lets "
            "the feeder's own account plant a definitions file")
        assert 'rm -f "$WEIGHT_MOUNT_DIR"/*.md' in script, (
            "stale bind-destination stubs are never swept")
        # ...and the sweep must come BEFORE the drop-in is written, or a rename
        # would leave the old stub beside the new bind for a whole run.
        assert (script.index('rm -f "$WEIGHT_MOUNT_DIR"/*.md')
                < script.index('emit_dropin "$DROPIN_DIR"'))


# ═══════════════════════════════════════════════════════════════════════════
# C. THE AUTOMATED CHECK-OFF — the SECOND post, on the LEGACY lane.
#
# The tracker item became `type: automated` / `check: weight`, so a feeder can
# tick it. Everything here exists because a tick is a CLAIM ABOUT A PERSON: it
# says they stood on a scale. The gauge can be wrong and merely look wrong; a
# tick that fires when the source is down tells the Owner they weighed in when
# they did not, and nothing on the panel would ever say otherwise.
# ═══════════════════════════════════════════════════════════════════════════

# 22:24 LOCAL on the same evening as the captured weigh-in (20:24 local, which
# is 01:24 UTC the NEXT day — the trap the capture proved). Two hours after the
# sample and still the same CIVIL day, which is the only day this feeder is
# ever willing to tick for.
SAME_DAY_NOW = CAPTURED_AT + 2 * 3600

# The Owner's file after the change that unblocked this block: the weigh-in
# item is `type: automated` with `check: weight`, and the goal is still the
# same `target`/`unit` beside it. The siblings stay, one of them carrying a
# `target` of its own.
AUTOMATED_MD = HEALTH_MD.replace(
    "  - id: weigh-in\n"
    "    title: Step on the scale\n"
    "    type: habit\n"
    "    recur: weekly\n",
    "  - id: weigh-in\n"
    "    title: Step on the scale\n"
    "    type: automated\n"
    "    check: weight\n"
    "    recur: weekly\n")
assert "type: automated" in AUTOMATED_MD, "the fixture edit must actually apply"

# THE SHAPE A FIRST-PASS TEST MISSES, AND IT IS THE LIKELY ONE. Reverting the
# item from a phone changes the TYPE column; it does not delete the CHECK
# column, because Drive sheet mode round-trips every item column it knows. So
# "back to a habit" on the Owner's screen arrives here as `type: habit` sitting
# NEXT TO `check: weight` - and a reader that only looked for `check:` would go
# on ticking an item NagLight no longer considers feeder-driven, earning a 400
# per weigh-in. The plain `HEALTH_MD` cannot catch that: it has no `check:` at
# all, so it is refused for the wrong reason.
HABIT_BUT_STILL_CHECKED_MD = AUTOMATED_MD.replace("type: automated", "type: habit")
assert "check: weight" in HABIT_BUT_STILL_CHECKED_MD


def feed_replies(*statuses):
    """A loopback /api/feed that answers each POST, in order, with a status.

    The order IS the assertion: the gauge is post #1 and the tick is post #2,
    so `feed_replies(200, 400)` is "the wall was told, the tick was refused".
    The bodies come back through `loopback_server`'s `seen`.
    """
    calls = {"n": 0}

    def respond(handler):
        index = calls["n"]
        calls["n"] += 1
        status = statuses[index] if index < len(statuses) else 200
        payload = b"{}"
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return respond


def captured_reader(payload=None):
    """A source reader returning a real WeightReading parsed from a real body."""
    body = CAPTURED_SHAPE if payload is None else payload
    return lambda env: feeder.parse_weight_datapoint(body, feeder.cycle_now(env))


def check_env(tmp_path, feed_url, defs=None):
    """A run_cycle environment with a real state dir and a real definitions dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return dict(
        _identity="sub-123", _feed_url=feed_url,
        WEIGHT_STATE_FILE=str(state_dir / "weight-state.json"),
        STATE_DIRECTORY=str(state_dir),
        WEIGHT_DEFINITIONS_DIR=write_defs(
            tmp_path, {"health.md": AUTOMATED_MD if defs is None else defs}))


def bodies(seen):
    return [json.loads(request["body"].decode()) for request in seen]


def a_goal(*_args):
    return (GOAL, "health.md")


def test_a_fresh_weigh_in_posts_the_gauge_and_then_the_tick_sr022(tmp_path):
    """THE HAPPY PATH, OVER REAL SOCKETS, END TO END.

    Two POSTs arrive at one /api/feed: the gauge FIRST, then the tick. The
    tick's body is asserted WHOLE — `{"check": ..., "ok": true}` and nothing
    else — because every extra field is a specific failure against NagLight's
    `handleAPIFeed`: a `kind` routes it to the gauge lane or earns "unknown
    feed kind"; a `color` or `rgb` makes it two signals where the handler
    demands exactly one; a `note` copies health data into the tracker's log
    lines; an `at` is silently IGNORED on this lane and would make back-dating
    look as though it worked.
    """
    with loopback_server(feed_replies(200, 200)) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": captured_reader()},
            goal_loader=a_goal)

    assert failures == []
    gauge, tick = bodies(seen)
    assert gauge["kind"] == "gauge" and gauge["id"] == "weight", "the gauge is first"
    assert tick == {"check": "weight", "ok": True}, (
        "the tick's body is exactly the two fields the legacy lane accepts")
    assert posted == [("weight", True, True), ("weight", None, True)]
    # ...and the Owner's weight is nowhere in the tick, nor is their id.
    assert "176" not in json.dumps(tick) and SENTINEL_ID not in json.dumps(tick)


def test_a_dead_source_reposts_the_gauge_and_never_ticks_sr022(tmp_path):
    """THE ONE THAT MATTERS MOST.

    The feeder wakes every 15 minutes and, when the source fails, re-posts the
    last real weight AT ITS ORIGINAL STAMP so the gauge stays honest and goes
    stale by itself. That path must post ONE body and no tick: a source that is
    down knows nothing about whether anybody stood on a scale, and a tick from
    there is a false statement about a person that the panel would render as
    an ordinary green check.
    """
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        # Cycle one: a real weigh-in, so there IS history to re-post later.
        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader()},
                         goal_loader=a_goal)
        first = len(seen)

        def dead(e):
            raise feeder.SourceFailure("google-health: HTTP 401")

        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW + 900, readers={"google-health": dead},
            goal_loader=a_goal)

    resent = bodies(seen)[first:]
    assert len(resent) == 1, "the re-post path must never tick"
    assert resent[0]["kind"] == "gauge"
    assert resent[0]["observed_at"] == feeder.iso8601_utc(CAPTURED_AT), (
        "and it is still the ORIGINAL stamp, not this cycle's")
    assert [key for key, fresh, _ok in posted if fresh is None] == []
    assert len(failures) == 1 and "401" in failures[0]


def test_an_unexpected_source_exception_never_ticks_either_sr022(tmp_path):
    """The other failure branch: not a SourceFailure but anything at all.

    `run_cycle` catches it, keeps `reading` None, and the tick must be gated on
    `reading`, never on "the cycle did not raise".
    """
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")

        def exploding(e):
            raise RuntimeError("boom")

        _posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": exploding},
            goal_loader=a_goal)

    assert [b for b in bodies(seen) if "check" in b] == []
    assert failures == ["google-health: unexpected RuntimeError"]


def test_the_same_weigh_in_is_ticked_once_however_many_cycles_run_sr022(tmp_path):
    """96 cycles a day against one weekly weigh-in must be ONE tick.

    `engine.Check` sets Done and saves the day unconditionally, so a repeated
    `ok: true` is a repeated `SaveDay`, a repeated panel wake and (on a
    single-user box) a repeated git commit, for a fact that has not changed.
    The mark is keyed on the SAMPLE TIME, not on "this cycle worked".
    """
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        for minute in (0, 15, 30, 45):
            feeder.run_cycle(
                env, now=SAME_DAY_NOW + minute * 60,
                readers={"google-health": captured_reader()},
                goal_loader=a_goal)

    ticks = [body for body in bodies(seen) if "check" in body]
    assert len(ticks) == 1, "one weigh-in, one tick, four cycles"
    assert len(bodies(seen)) == 5, "the gauge, however, is posted every cycle"


def test_a_second_weigh_in_the_same_day_is_ticked_again_sr022(tmp_path):
    """The mark is "which sample time", not "have we ever ticked".

    A strictly newer sample on the same civil day is a new weigh-in, and it
    ticks. This is the other side of the test above, and it is what stops the
    de-duplication being implemented as a latch that never reopens.
    """
    later_point = a_point(physical="2026-09-09T03:00:00Z", grams=FIXTURE_GRAMS + 90,
                          point_id="P2")
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader()},
                         goal_loader=a_goal)
        # 22:54 LOCAL, still the 8th: `now` must not cross local midnight or
        # the day gate would - correctly - refuse the second tick too.
        feeder.run_cycle(env, now=SAME_DAY_NOW + 30 * 60,
                         readers={"google-health": captured_reader(a_body(later_point))},
                         goal_loader=a_goal)

    assert len([b for b in bodies(seen) if "check" in b]) == 2


def test_a_weigh_in_from_another_day_is_never_ticked_sr022(tmp_path):
    """THE DAY GATE, AND WHY REFUSING IS THE HONEST ANSWER.

    The legacy lane cannot back-date: NagLight's `ok` path goes straight to
    `date := s.Now()` and never looks at `at` (only the colour/rgb path parses
    it). So a weigh-in recovered a day or two late — the feeder was down, the
    network was out — cannot be ticked onto the day it happened. Ticking it
    onto TODAY would write "weighed in" against a day nobody weighed in on.
    Missing the tick is recoverable with one tap on the panel; the false one is
    not, so this refuses.
    """
    yesterday = a_point(physical="2026-09-08T01:24:33Z", civil=False, point_id="P0")
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW,
            readers={"google-health": captured_reader(a_body(yesterday))},
            goal_loader=a_goal)

    assert [b for b in bodies(seen) if "check" in b] == []
    assert len(bodies(seen)) == 1, "the gauge is still posted - it is honest"
    assert failures == [], "and a day we will not tick for is not a failure"
    assert [k for k, fresh, _ in posted if fresh is None] == []


def test_a_reading_that_cannot_name_its_day_is_never_ticked_sr022(tmp_path):
    """No `civilTime` and no `utcOffset` is "we cannot say what day this was".

    `civil_date_of` answers None there rather than raising, and None must reach
    the tick as a refusal, not as a fall-through to `observed_at` — which is
    UTC and is a day out for every evening weigh-in in this timezone.
    """
    dayless = a_point(offset=None, civil=False, point_id="P3")
    reading = feeder.parse_weight_datapoint(a_body(dayless), SAME_DAY_NOW)
    assert feeder.reading_day_facts(reading) == (None, None)
    assert feeder.should_check_off(reading, None, SAME_DAY_NOW) is False

    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader(a_body(dayless))},
                         goal_loader=a_goal)
    assert [b for b in bodies(seen) if "check" in b] == []


def test_a_refused_tick_leaves_the_gauge_posted_and_is_retried_sr022(tmp_path):
    """INDEPENDENCE, FIRST DIRECTION: the tick fails, the gauge does not.

    The gauge is the promise (SN-040) and the tick is the extra, so the extra
    is posted SECOND and its 400 costs only itself. And because the mark is
    written only after a 200, the next cycle tries again rather than recording
    a tick that never landed.
    """
    with loopback_server(feed_replies(200, 400, 200, 200)) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": captured_reader()},
            goal_loader=a_goal)
        assert bodies(seen)[0]["kind"] == "gauge"
        assert posted[0] == ("weight", True, True), "the gauge landed"
        assert posted[1] == ("weight", None, False), "the tick did not"
        assert len(failures) == 1 and "post check weight" in failures[0]
        assert "400" in failures[0] and "body not logged" in failures[0]

        stored = json.loads((tmp_path / "state" / "weight-state.json").read_text())
        assert feeder.CHECK_STATE_KEY not in stored, (
            "a tick that did not land must not be remembered as done")

        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW + 900, readers={"google-health": captured_reader()},
            goal_loader=a_goal)
    assert failures == []
    assert posted[1] == ("weight", None, True), "retried on the next cycle"
    assert len([b for b in bodies(seen) if "check" in b]) == 2


def test_a_refused_gauge_does_not_prevent_the_tick_sr022(tmp_path):
    """INDEPENDENCE, SECOND DIRECTION, and it is the one an ordering bug hides.

    A gauge that 500s must not swallow the tick: they are two facts about the
    same weigh-in and neither is the other's precondition. Both are attempted
    and only the failure is reported.
    """
    with loopback_server(feed_replies(500, 200)) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": captured_reader()},
            goal_loader=a_goal)

    sent = bodies(seen)
    assert len(sent) == 2 and "check" in sent[1]
    assert posted[0] == ("weight", True, False)
    assert posted[1] == ("weight", None, True)
    assert len(failures) == 1 and failures[0].startswith("post weight:")


def test_the_tick_body_carries_no_kind_no_note_no_at_and_one_signal_sr022():
    """The wire contract of the legacy lane, asserted as four ABSENCES.

    Read straight off NagLight `internal/web/handlers.go`: `case "":` selects
    this struct, `signals != 1` is a 400, `Note` is decoded and then never read
    by anything, and `At` is parsed only inside
    `if body.Color != "" || body.RGB != ""`. A note would put a body weight
    into the tracker's log lines — which the traceability mirror pushes to a
    private repo hourly — for a field nothing displays.
    """
    body = feeder.check_body("weight")
    assert set(body) == {"check", "ok"}
    assert body["ok"] is True
    for absent in ("kind", "note", "at", "color", "rgb", "reason",
                   "value", "target", "observed_at"):
        assert absent not in body
    with pytest.raises(ValueError):
        feeder.check_body("   ")
    assert feeder.check_body("  weight  ")["check"] == "weight"


def test_the_feeder_never_posts_ok_false_sr022():
    """There is no circumstance in which this feeder knows someone did NOT
    weigh in, and `ok: false` would call `engine.Uncheck` and silently erase a
    tick the Owner made by hand. Asserted against the source, because the
    absence of a branch cannot be exercised."""
    source = (REPO / "stack" / "weight" / "weight_feeder.py").read_text(encoding="utf-8")
    assert '"ok": False' not in source and "'ok': False" not in source
    assert source.count('"ok": True') == 1


def test_the_tick_mark_lives_in_the_one_existing_state_file_sr022(tmp_path):
    """STATE, RULE 6: one file, the existing one, through the existing guard.

    `open_for_write` allows exactly one path plus its `.tmp`, realpath-resolved
    and contained in the StateDirectory, opened O_NOFOLLOW. A second state file
    would be a second blessed path, so the mark shares this one — and the file
    still holds nothing but weights, timestamps and a check id.
    """
    state_dir = tmp_path / "state"
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader()},
                         goal_loader=a_goal)

    assert sorted(p.name for p in state_dir.iterdir()) == ["weight-state.json"], (
        "no second state file, and no .tmp left behind")
    state_file = state_dir / "weight-state.json"
    stored = json.loads(state_file.read_text())
    assert stored[feeder.CHECK_STATE_KEY] == {"weight": CAPTURED_AT}
    assert sorted(stored["weight"]) == ["observed_at", "value"]
    assert SENTINEL_ID not in json.dumps(stored)
    assert "REFRESH-TOKEN" not in json.dumps(stored)
    # ...and it round-trips, or gate 2 would silently stop working.
    reloaded = feeder.load_state(str(state_file), SAME_DAY_NOW)
    assert reloaded[feeder.CHECK_STATE_KEY] == {"weight": CAPTURED_AT}


def test_the_tick_mark_is_written_by_the_credential_guard_sr022(tmp_path):
    """The mark did not get its own writer. When `open_for_write` refuses, the
    mark is lost with the reading — one guard, one file, one failure line — and
    the gauge is still posted because it went first."""
    state_dir = tmp_path / "state"
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")
        # OUTSIDE the StateDirectory: the containment half of the guard.
        env["WEIGHT_STATE_FILE"] = str(tmp_path / "elsewhere.json")
        _posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": captured_reader()},
            goal_loader=a_goal)

    assert len(bodies(seen)) == 2, "both posts still happened"
    assert len(failures) == 1 and failures[0].startswith("state ")
    assert "PermissionError" in failures[0]
    assert not (tmp_path / "elsewhere.json").exists()
    assert not (tmp_path / "elsewhere.json.tmp").exists()


def test_a_tick_mark_that_is_not_credible_is_dropped_sr022():
    """The state file is INPUT, not memory, and its marks get the same
    treatment its readings get. A mark in the FUTURE is the dangerous one: it
    would suppress every real tick until the clock caught up, so it is refused
    by the same rule that refuses a future reading."""
    marks = feeder.validate_checked_marks(
        {"weight": CAPTURED_AT,
         "future": SAME_DAY_NOW + 10 * 3600,
         "ancient": feeder.EPOCH_FLOOR - 1,
         "text": "yesterday",
         "": CAPTURED_AT}, SAME_DAY_NOW)
    assert marks == {"weight": CAPTURED_AT}
    assert feeder.validate_checked_marks(["weight"], SAME_DAY_NOW) == {}
    assert feeder.validate_checked_marks(None, SAME_DAY_NOW) == {}


def test_a_lost_tick_mark_cannot_tick_the_wrong_day_sr022():
    """The two gates are independent, and this is what that buys.

    Lose the mark — a corrupted file, a person with an editor — and the day
    gate still holds: yesterday's weigh-in is not ticked onto today just
    because we have forgotten ticking it.
    """
    yesterday = a_point(physical="2026-09-08T01:24:33Z", civil=False, point_id="P0")
    stale = feeder.parse_weight_datapoint(a_body(yesterday), SAME_DAY_NOW)
    assert feeder.should_check_off(stale, None, SAME_DAY_NOW) is False
    fresh = feeder.parse_weight_datapoint(CAPTURED_SHAPE, SAME_DAY_NOW)
    assert feeder.should_check_off(fresh, None, SAME_DAY_NOW) is True
    assert feeder.should_check_off(fresh, CAPTURED_AT, SAME_DAY_NOW) is False
    assert feeder.should_check_off(fresh, CAPTURED_AT - 1, SAME_DAY_NOW) is True
    assert feeder.should_check_off(None, None, SAME_DAY_NOW) is False


def test_reverting_the_item_to_a_habit_stops_the_tick_sr022(tmp_path):
    """RULE 7, THE KNOB QUESTION, ANSWERED BY THE DEFINITIONS INSTEAD.

    `type:` and `check:` are the declaration. They belong to the person, they
    round-trip through Drive sheet mode as item columns, and they are what
    NagLight itself reads. So there is no `WEIGHT_CHECK_ENABLED`: putting the
    item back to `type: habit` from a phone turns the tick off by itself on the
    next sync, and no hub knob can contradict it in either direction.
    """
    for name in "abcde":
        (tmp_path / name).mkdir()
    automated = write_defs(tmp_path / "a", {"health.md": AUTOMATED_MD})
    habit = write_defs(tmp_path / "b", {"health.md": HEALTH_MD})
    empty = write_defs(tmp_path / "c", {})
    assert feeder.check_id_from_definitions(automated) == "weight"
    assert feeder.check_id_from_definitions(habit) is None, "type: habit does not tick"
    # ...and the shape a revert actually produces: the check column SURVIVES
    # the type column changing, so `type:` is what has to be read.
    reverted = write_defs(tmp_path / "e", {"health.md": HABIT_BUT_STILL_CHECKED_MD})
    assert feeder.check_id_from_definitions(reverted) is None, (
        "`type: habit` beside a lingering `check:` must not tick")
    assert feeder.check_id_from_definitions(empty) is None
    assert feeder.check_id_from_definitions("/nonexistent/definitions") is None
    # An `automated` item with no `check:` names no feeder, so nothing ticks it.
    no_check = write_defs(tmp_path / "d", {
        "health.md": AUTOMATED_MD.replace("    check: weight\n", "")})
    assert feeder.check_id_from_definitions(no_check) is None
    # ...and the goal still reads out of the SAME item, unchanged by all this.
    assert feeder.load_goal_from_definitions(automated)[0] == DEFS_GOAL


def test_a_habit_item_ticks_nothing_through_a_whole_cycle_sr022(tmp_path):
    """The same negative, end to end: the Owner's PREVIOUS definitions file
    posts the gauge and nothing else, with no failure line to decode."""
    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed",
                        defs=HABIT_BUT_STILL_CHECKED_MD)
        posted, failures, _ = feeder.run_cycle(
            env, now=SAME_DAY_NOW, readers={"google-health": captured_reader()},
            goal_loader=a_goal)
    assert len(bodies(seen)) == 1 and "check" not in bodies(seen)[0]
    assert posted == [("weight", True, True)]
    assert failures == []


def test_a_dead_source_does_not_even_read_the_definitions_sr022(tmp_path):
    """The tick is gated on a real read TWICE, and this is the outer gate.

    `run_cycle` does not consult the definitions at all on a cycle that could
    not read the source. `should_check_off` would refuse such a cycle anyway,
    so without this the outer gate is untested and a mutant that removes it
    survives - which is exactly what the first mutation round found. What it
    buys is real: 95 cycles a week do no definitions I/O, and the gate the
    reader sees first says plainly that a dead source cannot reach the tick.
    """
    asked = []

    def counting_check_loader(defs_dir, *_a):
        asked.append(defs_dir)
        return "weight"

    with loopback_server(feed_replies()) as (feed, seen):
        env = check_env(tmp_path, feed + "/api/feed")

        def dead(e):
            raise feeder.SourceFailure("google-health: HTTP 401")

        feeder.run_cycle(env, now=SAME_DAY_NOW, readers={"google-health": dead},
                         goal_loader=a_goal, check_loader=counting_check_loader)
        assert asked == [], "a dead source must not even ask what to tick"

        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader()},
                         goal_loader=a_goal, check_loader=counting_check_loader)
    assert len(asked) == 1, "and a live one asks exactly once"
    assert bodies(seen)[-1] == {"check": "weight", "ok": True}


def test_the_check_id_is_whatever_the_item_declares_sr022(tmp_path):
    """It is not hardcoded. Renaming `check:` renames what is posted, which is
    what "the definitions are the declaration" has to mean."""
    renamed = write_defs(tmp_path, {
        "health.md": AUTOMATED_MD.replace("check: weight", "check: body-weight")})
    assert feeder.check_id_from_definitions(renamed) == "body-weight"
    with loopback_server(feed_replies()) as (feed, seen):
        env = dict(_identity="sub-123", _feed_url=feed + "/api/feed",
                   WEIGHT_STATE_FILE=str(tmp_path / "s.json"),
                   STATE_DIRECTORY=str(tmp_path),
                   WEIGHT_DEFINITIONS_DIR=renamed)
        feeder.run_cycle(env, now=SAME_DAY_NOW,
                         readers={"google-health": captured_reader()},
                         goal_loader=a_goal)
    assert bodies(seen)[1] == {"check": "body-weight", "ok": True}


def test_no_hub_knob_can_turn_the_check_off_on_or_off_sr022():
    """The negative, by file scan, exactly as the goal's negative is asserted.

    A knob here would be a second place the same intent lives, and it would
    disagree with the definitions the first time the Owner changed their mind
    from their phone.
    """
    files = [REPO / "stack" / ".env.example"]
    schema = REPO.parent / "HomeHub" / "scripts" / "deploy" / "FieldSchema.psd1"
    if schema.exists():
        files.append(schema)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for knob in ("WEIGHT_CHECK_ENABLED", "WEIGHT_CHECK_ID", "WEIGHT_AUTO_CHECK"):
            assert knob not in text, "%s appears in %s" % (knob, path.name)


def test_the_goal_and_the_check_read_the_same_files_by_the_same_rules_sr022(tmp_path):
    """ONE walk, ONE containment rule. A definitions file that links OUT of the
    directory is refused for the check exactly as it is for the goal — and the
    check's refusal is SILENCE rather than an exception, because the tick may
    never take the gauge down with it."""
    outside = tmp_path / "outside.md"
    outside.write_text(AUTOMATED_MD, encoding="utf-8")
    defs_dir = write_defs(tmp_path, {"real.md": AUTOMATED_MD})
    link = Path(defs_dir) / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make a symlink without privilege")
    with pytest.raises(ValueError):
        feeder.load_goal_from_definitions(defs_dir)
    assert feeder.check_id_from_definitions(defs_dir) is None


def test_the_tick_is_stamped_by_naglights_clock_and_cannot_be_back_dated_sr022():
    """WHICH DAY THE TICK LANDS ON — written down because it is NOT always the
    day of the weigh-in, and pretending otherwise would be the lie this block
    is about.

    Established by READING NagLight's `handleAPIFeed`, not by assuming:

      * the `ok` path never touches `body.At`. `At` is parsed (as RFC3339) only
        inside `if body.Color != "" || body.RGB != ""`, into a logfile.Report.
        The boolean path falls through to `date := s.Now()`.
      * `s.Now()` defaults to `todayString`, which is
        `time.Now().Format("2006-01-02")` — the TRACKER CONTAINER's local date.
      * `stack/docker-compose.yml`'s `tracker:` service sets no `TZ:`, while
        every other service that cares sets `TZ: ${TIMEZONE}`. So that date is
        UTC today.

    Consequence, for the captured shape: a weigh-in at 20:24 local on Monday is
    01:24 UTC on Tuesday, so the tick lands on TUESDAY's log. This feeder
    cannot fix that from here — the lane carries no back-dating — so it does
    not send an `at` that would silently be dropped, and the residual
    off-by-one is written down in stack/weight/README.md and docs/status.md for
    the coordinator instead.
    """
    assert "at" not in feeder.check_body("weight")
    compose = (REPO / "stack" / "docker-compose.yml").read_text(encoding="utf-8")
    tracker = compose[compose.index("\n  tracker:"):compose.index("\n  actual:")]
    assert "TZ: ${TIMEZONE}" not in tracker, (
        "if the tracker gains a TZ the day story changes and the README must "
        "be revisited - that is the fix, and it is the coordinator's to make")
    # The trap itself, restated on the numbers the capture proved.
    assert feeder.local_civil_date(CAPTURED_AT, -18000) == FIXTURE_CIVIL_DAY
    assert time.strftime("%Y-%m-%d", time.gmtime(CAPTURED_AT)) == FIXTURE_UTC_DAY
