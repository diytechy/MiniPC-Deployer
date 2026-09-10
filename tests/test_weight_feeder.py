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


def test_the_block_is_still_blocked_by_construction_sr022():
    """None of this round's fixes may have opened a path to a fabricated
    reading. `read_google_health` still refuses, and no parser has appeared."""
    for name in ("parse_google_health", "parse_weight_datapoint",
                 "parse_weight", "parse_datapoints"):
        assert not hasattr(feeder, name)
    with pytest.raises(feeder.SourceFailure):
        feeder.read_google_health({"_now": NOW})
