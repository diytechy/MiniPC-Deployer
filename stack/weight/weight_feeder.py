#!/usr/bin/env python3
"""The hub's weight feeder: one body-weight reading -> one NagLight gauge.

ONE RESPONSIBILITY: post the household member's latest body weight, against the
goal THEY declared in their own definitions, as the `weight` gauge NagLight
already serves (SN-040, SR-067). It decides nothing about colour, invents no
goal, and holds no credential of its own.

WHY A PLAIN HUB SERVICE AND NOT A CONTAINER - the same reasoning A40 applied to
the AI CLI service (SR-019) and B7 applied to the usage feeder (SR-021): the
vendor half is an OAuth refresh token a human minted interactively through a
browser. A container would have to mount the directory it lives in, which
punctures the isolation at the only point that mattered.

═══════════════════════════════════════════════════════════════════════════════
THE GATE IS CLEARED. A REAL RESPONSE BODY WAS CAPTURED ON 2026-09-09, AND THE
PARSER IN THIS FILE IS WRITTEN AGAINST IT.
═══════════════════════════════════════════════════════════════════════════════

`weight_oauth.py capture` was run by the Owner against the live API on
2026-09-09 and got HTTP 200, 712 bytes, from

    GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints

with NO query parameters at all. THE BODY ITSELF IS NOT IN THIS REPO AND NEVER
WILL BE - it carries the Owner's Google user id and their real body weight,
which is health data about a specific person living in this house. What is
recorded here is its SHAPE, with a placeholder id and a made-up weight:

    {
      "dataPoints": [
        {
          "name": "users/<GOOGLE-USER-ID>/dataTypes/weight/dataPoints/<POINT-ID>",
          "dataSource": {"recordingMethod": "MANUAL", "platform": "FITBIT"},
          "weight": {
            "sampleTime": {
              "physicalTime": "2026-09-09T01:24:33.390135Z",
              "utcOffset": "-18000s",
              "civilTime": {
                "date": {"year": 2026, "month": 9, "day": 8},
                "time": {"hours": 20, "minutes": 24, "seconds": 33,
                         "nanos": 390135000}
              }
            },
            "weightGrams": 79832
          }
        }
      ]
    }

FOUR THINGS THE CAPTURE SETTLED, EACH OF WHICH PROSE HAD GUESSED WRONG OR NOT
GUESSED AT ALL:

  1. THE TOP-LEVEL KEY IS `dataPoints`, PLURAL. Earlier prose in this repo (and
     the README's own troubleshooting table) said `dataPoint`. The observed
     body says `dataPoints`, which is also what the discovery document's
     `ListDataPointsResponse` says. The observation wins.
  2. `weightGrams` IS GRAMS, and the observed value converts to a body weight
     the Owner recognises. That is the one fact this whole gate existed for:
     grams read as kilograms or as pounds does not fail, it posts a confident,
     plausible, WRONG body weight and nothing on the wall could contradict it.
     `grams_to_pounds` is the single conversion and `check_vendor_grams` is the
     single place it is called from a vendor body.
  3. `physicalTime` AND `civilTime` DISAGREE ABOUT THE DAY, AND THAT IS NOT A
     BUG. In the captured body `physicalTime` is 2026-09-09T01:24Z while
     `civilTime` is 2026-09-08 20:24 local, because `utcOffset` is -18000s.
     The weigh-in happened on MONDAY EVENING; in UTC it is TUESDAY. So:
       * the GAUGE's `observed_at` is `physicalTime` - the instant the reading
         was true, which is what NagLight's staleness rule keys off;
       * anything that ever needs the reading's CALENDAR DAY - a future
         auto-check-off of the `weigh-in` habit, which is what SN-040 hints at
         next - MUST use `civilTime` (or `physicalTime` shifted by
         `utcOffset`), NEVER `physicalTime` alone. Using `physicalTime` would
         tick off Tuesday for a Monday-evening weigh-in, every single time
         anyone in this timezone stands on a scale after 7pm.
     `WeightReading` therefore carries `civil_date` and `utc_offset_seconds`
     alongside the pounds and the stamp. No check-off is built here; the parser
     simply does not throw away what such a caller would need.
  4. THE OBSERVED RESPONSE CARRIED NO `nextPageToken`. What this feeder does
     when one IS present is stated at `list_weight_data_points`, and it is
     marked there as an ASSUMPTION rather than an observation.

Everything below this line was established on 2026-09-09 by CALLING Google,
not by reading a blog, and remains true:

  * `GET https://www.googleapis.com/discovery/v1/apis?preferred=false` -> 200,
    531 APIs, of which `health:v4` (and `health:v4beta`) are listed with
    `discoveryRestUrl https://health.googleapis.com/$discovery/rest?version=v4`.
    So the API exists under exactly the name the plan used. It is NOT Google
    Fit (`fitness:v1`, closed to new sign-ups) and NOT Health Connect (which is
    Android-device-local and has no server-side REST path at all); it is the
    successor to the Fitbit Web API, fed from Fitbit, Pixel Watch and partner
    apps.

  * `GET https://health.googleapis.com/$discovery/rest?version=v4` -> 200,
    292545 bytes, `revision 20260907`, `rootUrl https://health.googleapis.com/`.
    The Weight data type is REAL and its shape is exact:

        "Weight": {"description": "Body weight measurement.", "properties": {
            "sampleTime":  {"$ref": "ObservationSampleTime"},   # required
            "weightGrams": {"type": "number", "format": "double"},  # required
            "notes":       {"type": "string"}}}

    reached as the `weight` member of the `DataPoint` union, and

        "ObservationSampleTime": {"properties": {
            "physicalTime": {"format": "google-datetime"},   # required, RFC3339
            "utcOffset":    {"format": "google-duration"},   # required
            "civilTime":    {"$ref": "CivilDateTime", "readOnly": true}}}

  * The read method is
        GET v4/users/{usersId}/dataTypes/{dataTypesId}/dataPoints
    i.e. `GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints`,
    whose `parent` parameter documentation gives `users/me/dataTypes/weight` as
    a worked example, with a `filter` of
        weight.sample_time.physical_time >= "<RFC3339>"
    and a response of `ListDataPointsResponse {dataPoints[], nextPageToken}`.
    NOTE that developers.google.com's migration page renders this path as
    `/v4/users/me/dataPoints/weight`, which does NOT match the discovery
    document. The discovery document is the machine-readable contract and is
    what a client is routed by; the prose page is wrong. This is precisely why
    the build plan makes a real call a gate.

  * ONE REAL CALL WAS MADE against that URL, unauthenticated:
        HTTP 401 UNAUTHENTICATED, "Request is missing required authentication
        credential", with
        details[0].metadata = {"service": "health.googleapis.com",
          "method": "google.devicesandservices.health.v4.DataPointsService.ListDataPoints"}
    Routing happens BEFORE authentication, so a 401 that names the backend RPC
    method proves the route resolves to a real service. A second call with a
    junk bearer returned the same 401 with "invalid authentication credentials".

  * THE SCOPE IS NOT WHAT THE PLAN ASSUMED. There is NO weight-specific scope.
    The discovery document lists 21 scopes and the one that admits the weight
    data type is
        https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
    which also covers blood glucose, body fat, oxygen saturation, heart-rate
    metrics and core body temperature. Granting it grants ALL of those. That is
    a real widening of what the household hands this box and it is the Owner's
    call, not this feeder's.

ALL THREE OWNER STEPS ARE NOW DONE (2026-09-09): health.googleapis.com is
enabled on the Google Cloud project that owns the household's one existing
OAuth client - oauth2-proxy's, in OAUTH2_PROXY_CLIENT_ID /
OAUTH2_PROXY_CLIENT_SECRET (there is no separate TRACKER_DRIVE_CLIENT_* pair on
this hub; it is accepted as a fallback for a differently-provisioned box and
nothing more); the scope above is on that client's consent screen and the Owner
is a Test user; and a refresh token has been minted into WEIGHT_TOKEN_FILE.

WHAT REMAINS UNEXERCISED, SO THAT NOBODY READS MORE INTO THIS THAN HAPPENED.
ONE list call has been made, and it returned ONE data point. The paging branch,
the refresh-token grant as this FEEDER performs it (as opposed to as
`weight_oauth.py capture` performs it), a 401 from an expired token and a
multi-point history are all reasoned-about rather than observed, and each is
marked as such where it lives. Every one of them fails towards the same place:
`SourceFailure` -> the unavailable gauge -> the last real reading at its
original stamp. Nothing on any of those paths can invent a number.

═══════════════════════════════════════════════════════════════════════════════

THE WIRE SHAPE, AND WHY IT DIFFERS FROM THE USAGE FEEDER'S (NagLight IF-012,
this repo's IF-014):
  * unit is `lb`, and `lb` is THE ONE unit NagLight infers a range for
    (internal/gauge.unitRangeSpan: a 50 lb bar, goal +/-25). So min/max are
    DELIBERATELY OMITTED - see GAUGE_MIN_MAX_OMITTED below.
  * there is NO `window` and therefore NO `direction`. A goal is a standing
    line you are measured against on any day, not a pace across a period, and
    NagLight REFUSES a direction without a window.
  * `value` and `target` are always present. This is the case the 2026-09
    contract change was made for: an omitted weight used to be stored as 0, and
    "0 lb" against a 180 lb goal is the most alarming-looking green-adjacent
    lie the panel could tell.

THE GOAL LIVES IN THE USER'S DEFINITIONS, AS THE `target` OF ONE ITEM - see
the block above DEFAULT_GOAL_CATEGORY, and `load_goal_from_definitions`. It
used to be a top-level `weight_goal_lb` key; that was changed on 2026-09-09
because Drive SHEET mode, which this household runs, regenerates the .md
from an item-column list and erases unknown top-level keys. Do not put it
back.

THE FOUR ACCEPTANCE PROPERTIES, each with the symbol that enforces it:
  1. a missing or stale source renders stale, never green -> `build_post`
                                                             (+ `is_fresh`)
  2. the goal comes from the user's definitions, never from a hub knob and
     never from a default                     -> `load_goal_from_definitions`
  3. never writes a credential or a token file -> `open_for_write`
  4. off by default, and refuses to guess an identity or a destination
                              -> `resolve_enabled` / `resolve_identity` /
                                 `resolve_feed_url`

Implements: SR-022, LLR-006
"""

import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── The wire contract (NagLight IF-012 / this repo's IF-014) ─────────────────
# Not tunables. This is the shape POST /api/feed accepts for a weight gauge,
# and each line is a 400 if it is wrong.
GAUGE_ID = "weight"          # the SAME id B5's feeders/weight-manual.ps1 posts,
                             # so this REPLACES that manual feed by upsert
                             # rather than standing a second bar beside it.
GAUGE_LABEL = "Weight"
GAUGE_ICON = "scale"
GAUGE_UNIT = "lb"
RUNE_LIMIT = 120             # id/label/icon/unit are capped in RUNES, not bytes.

# WHY NO min/max IS SENT, AND WHY THAT IS A DECISION RATHER THAN AN OMISSION.
# NagLight's internal/gauge.unitRangeSpan lists exactly one unit whose natural
# bar width the household has agreed on: `lb`, a 50 lb range centred on the
# goal. Omitting both ends therefore asks the tracker for the agreed bar, and
# the alternative - computing goal+/-25 here - would put a SECOND range
# authority on the producer side, so a later change to the household's agreed
# width would silently apply to the manual feeder and not to this one. The
# usage feeder had to send min/max for the opposite reason: `%` is not in that
# table, so omitting them there is a refusal.
GAUGE_MIN_MAX_OMITTED = True

# NagLight's staleness horizons by window kind, kept here because the cadence
# has to be chosen against them. This gauge has NO window, so it takes the
# `static` horizon: 7 days. That is a long time for a wall panel to keep
# showing a number, which is why WEIGHT_INTERVAL is set far below it and a test
# reads the shipped timer file and fails if it is ever loosened.
STALE_HORIZON_SECONDS = {
    "daily": 36 * 3600,
    "weekly": 24 * 3600,
    "monthly": 48 * 3600,
    "static": 7 * 24 * 3600,
    None: 7 * 24 * 3600,
}

# WHERE THE GOAL LIVES: THE `target` OF ONE ITEM IN THE PERSON'S DEFINITIONS.
#
# THIS CHANGED ON 2026-09-09 AND THE REASON MUST NOT BE LOST (see docs/status.md,
# "the goal moved from a file-level key to the weigh-in item's target"). The
# first version put the goal in a TOP-LEVEL frontmatter key, `weight_goal_lb`,
# and deliberately refused to read an item field. Two facts killed that design:
#
#   * THIS HOUSEHOLD RUNS SHEET MODE (TRACKER_DRIVE_SHEET_ID set,
#     TRACKER_DRIVE_FOLDER_ID deliberately blank). In sheet mode NagLight's
#     internal/defsheet REGENERATES each definitions .md from a fixed `columns`
#     list, which is the ITEM field set; a column it does not recognise is
#     collected into `unknown` and DROPPED. A top-level `weight_goal_lb` is
#     therefore erased by the first sync after anyone edits the sheet. The old
#     design did not merely lack a feature - it silently deleted the household's
#     goal and left the panel dark with nothing saying why.
#   * THERE IS NO VENDOR FALLBACK. Google Health v4 has no goal or target
#     concept at all: `DataPoint` has 43 members and none is a goal, `Profile`
#     and `Settings` carry none, and the only two occurrences of "goal" in the
#     292KB discovery document (revision 20260908) are a UI settings enum.
#
# `target` and `unit` are ALREADY item columns that round-trip through sheet
# mode today, so the goal now lives where the sync will actually carry it:
#
#     ---
#     category: Health
#     color_weight: 1.5
#     items:
#       - id: weigh-in
#         title: Step on the scale
#         type: habit
#         recur: weekly
#         horizon: long
#         target: 170
#         unit: lb
#     ---
#
# DO NOT "RESTORE" THE TOP-LEVEL KEY. It is still recognised, but only so the
# feeder can REFUSE and say where the goal moved to - see
# `load_goal_from_definitions`.

# WHICH item, as two LOCATION knobs rather than two hardcoded strings, so the
# Owner can rename or move the item without a code change. They name a place,
# never a number: there is still no goal-shaped knob anywhere deploy reads, and
# a test asserts that negative by file scan.
DEFAULT_GOAL_CATEGORY = "Health"
DEFAULT_GOAL_ITEM_ID = "weigh-in"

# The item fields read, and the ONE unit accepted. See `goal_from_item`: the
# unit is CHECKED, never assumed, because 77 is a perfectly plausible kilogram
# weight AND sits inside the pounds sanity band, so an unchecked `unit: kg`
# posts "77 lb" - a confident, plausible, wrong number about someone's body,
# which is the same failure class this feeder still refuses to write a vendor
# parser for.
TARGET_KEY = "target"
UNIT_KEY = "unit"
GOAL_UNIT = "lb"

# ── The automated check-off: the SECOND post, on the LEGACY lane ─────────────
# The SAME item that carries the goal also declares, in the Owner's own
# definitions, whether a feeder ticks it at all:
#
#     - id: weigh-in
#       type: automated        <- TYPE_KEY / AUTOMATED_TYPE
#       check: weight          <- CHECK_KEY: the feeder check id NagLight
#       recur: weekly             matches against model.Item.Check
#       target: 170
#       unit: lb
#
# THERE IS DELIBERATELY NO `WEIGHT_CHECK_ENABLED` KNOB, and the reason is the
# one that put the goal in the definitions rather than in stack/.env (SN-040).
# `type:` and `check:` ARE the declaration. They are the person's, they
# round-trip through Drive sheet mode as item columns, and they are exactly
# what NagLight itself reads to decide an item is feeder-driven. A hub knob
# beside them would be a second place the same intent lives, and the two would
# disagree the first time the Owner changed their mind from their phone: a knob
# saying "yes" over an item that says `type: habit` earns a 400 every cycle,
# and a knob saying "no" over `type: automated` leaves an item nothing can ever
# tick and no hint why. Reverting the item to `type: habit` turns this off by
# itself on the next sync. So nothing is added to stack/.env.example, and
# nothing is needed in HomeHub's FieldSchema.psd1 (which is out of this block's
# bounds anyway).
TYPE_KEY = "type"
CHECK_KEY = "check"
AUTOMATED_TYPE = "automated"

# Where the "which sample time have we already ticked?" marks live INSIDE the
# one existing state file: a dict of {check id: epoch seconds} alongside the
# gauge's own entry, written by the same `save_state` through the same
# `open_for_write` guard. It is NOT a second state file on purpose - a second
# file is a second path the credential allow-list would have to bless.
CHECK_STATE_KEY = "checked"

# The superseded top-level key. Kept ONLY so a file that still carries it is
# refused with a message that says where the goal went. See above.
LEGACY_GOAL_KEY = "weight_goal_lb"

# A goal outside this band is a typo, not a goal. 40 lb is below any living
# adult and 1000 lb is above the heaviest ever recorded; the point is not to
# police anyone's body but to catch `1800` typed for `180`, which would drag
# the inferred bar to 1775..1825 and paint a real reading full red forever.
# ONE HOME FOR THE BAND, TWO NAMES FOR ITS TWO USES. The same reasoning
# applies to a READING as to a goal - `-500` and `100000` are not body weights
# either - and a second copy of the numbers is a second thing to keep in step.
PLAUSIBLE_LB = (40.0, 1000.0)
GOAL_MIN_LB, GOAL_MAX_LB = PLAUSIBLE_LB

# THE HUB AND THE VENDOR DO NOT SHARE A CLOCK, so every comparison between a
# vendor's timestamp and ours gets this much slack and no more. Small on
# purpose: its job is to absorb NTP jitter, not to let a future stamp through.
MAX_CLOCK_SKEW_SECONDS = 300

# No timestamp this feeder can legitimately see predates the block that wrote
# it (2025-01-01). A stored stamp below this is corruption, not history.
EPOCH_FLOOR = 1735689600

# The directory this service's own StateDirectory= gives it. The unit ships
# `StateDirectory=homehub-weight`, so systemd exports $STATE_DIRECTORY as
# exactly this; the literal is the fallback for a hand-run cycle, so a hand-run
# is bound the same way rather than left unbound. See `open_for_write`.
DEFAULT_STATE_ROOT = "/var/lib/homehub-weight"


class SourceFailure(Exception):
    """The weight source could not be read, or answered with something unusable.

    ONE exception for every failure class the acceptance criterion names -
    absent token, transport error, timeout, 401, garbage body, a well-formed
    body with no weight in it, an out-of-range number, and a bug in our own
    code. They are one type on purpose: `build_post` must treat them
    identically, and a second exception type is a second chance to accidentally
    post a fresh reading for a source that failed.
    """


class NoWeightYet(SourceFailure):
    """The source answered, correctly, that this account has logged no weight.

    A SUBCLASS OF SourceFailure ON PURPOSE, AND THE DISTINCTION IS IN THE TYPE
    AND THE SENTENCE, NEVER IN THE OUTCOME. Being a subclass is what keeps the
    module's central promise intact: `run_cycle` catches SourceFailure, so an
    empty history takes the ORDINARY unavailable path - value 0 with no
    `observed_at`, or the last real reading at its original stamp - exactly as a
    401 or a dead network would. It is not a crash, it does not abort the cycle,
    and it emphatically does not post 0 lb as a measurement.

    Being a distinct type, with its own sentence, is what stops the two being
    confused by a HUMAN. "Google returned 200 and you have never logged a
    weight" and "the token expired" are the same gauge and completely different
    errands: one is answered by standing on a scale, the other by running
    `weight_oauth.py mint --force`. A journal line that said "google-health:
    HTTP 200" for the first would send the Owner hunting a fault that is not
    there, and one that said "no reading" for the second would leave a broken
    credential looking like a lifestyle choice.

    Implements: LLR-006
    """


class GoalMissing(Exception):
    """The user's definitions declare no goal.

    DELIBERATELY NOT A SourceFailure. A missing SOURCE means "we do not know
    what you weigh", and the honest answer to that is an unavailable gauge. A
    missing GOAL means "you have not said what you are aiming at", and there is
    no honest gauge to draw at all - the target line IS the goal, and NagLight
    refuses a post without a target. Inventing one would draw a bar around a
    number nobody chose and colour it green or red on that invention.
    """


class EgressRefused(Exception):
    """A request tried to leave this box by a route the feeder does not permit.

    Raised by the redirect refusal and the peer-address guard, and NOT a
    subclass of SourceFailure: `post_gauge` converts it deliberately, and
    nothing may catch it by accident on the way out.
    """


# ── Pure core: no I/O below this line until the SHELL banner ────────────────

def check_pounds(raw, where):
    """Return `raw` as a float weight in pounds, or raise SourceFailure.

    Contract:
      Inputs:  raw: anything a source sent; where: str for the message.
      Outputs: float, strictly positive and finite.
      Raises:  SourceFailure for None, a string, a bool, NaN, +/-inf, zero and
               negatives.

    `bool` is rejected explicitly because `isinstance(True, int)` is True in
    Python, so `True` would otherwise sail through as 1 lb.

    Implements: LLR-006
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SourceFailure("%s: weight is not a number: %r" % (where, raw))
    value = float(raw)
    if not math.isfinite(value):
        raise SourceFailure("%s: weight is not finite: %r" % (where, raw))
    if value <= 0.0:
        raise SourceFailure("%s: weight is not positive: %r" % (where, raw))
    return value


def check_plausible_weight_lb(value, where):
    """Return `value` as a float weight, or raise SourceFailure.

    `check_pounds` only asks whether the number is finite and positive, which
    is right for the grams it also guards but is not enough for the number that
    reaches the panel: `100000` and `0.001` are both finite and positive, and
    both would draw a real 50 lb bar somewhere nobody's body is. The band is
    the same one a GOAL is held to (PLAUSIBLE_LB), for the same reason - the
    point is not to police anyone's body but to catch a units error or a
    corrupted state file before it becomes a confident wrong number on a wall.

    Implements: LLR-006
    """
    pounds = check_pounds(value, where)
    low, high = PLAUSIBLE_LB
    if pounds < low or pounds > high:
        raise SourceFailure(
            "%s: %r lb is outside the plausible band %g..%g, so it is a units "
            "error or corruption rather than a body weight"
            % (where, value, low, high))
    return pounds


def check_observed_at(stamp, now, where):
    """Return the epoch second a reading was TRUE, or raise SourceFailure.

    Contract:
      Inputs:  stamp: epoch seconds, as the source reported them; now: this
               cycle's clock; where: str for the message.
      Outputs: int epoch seconds.
      Raises:  SourceFailure for a non-number, a stamp before EPOCH_FLOOR, and
               a stamp in the FUTURE.

    A FUTURE STAMP IS THE ONE THAT FABRICATES FRESHNESS. `build_post` posts a
    reading at the moment it was true precisely so that an old weigh-in renders
    stale; a stamp ahead of the clock defeats that from the other side and
    keeps a dead source green until the stamp itself expires. It is refused
    rather than clamped to `now`, because clamping would invent the very thing
    the invariant forbids - a stamp this cycle did not measure.

    An OLD stamp is NOT an error here. A fortnight-old weigh-in is a true
    reading that NagLight renders stale, and that is the correct outcome: the
    person has not stepped on the scale.

    Implements: LLR-006
    """
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) \
            or not math.isfinite(stamp):
        raise SourceFailure("%s: observed_at is not a number: %r" % (where, stamp))
    value = int(stamp)
    if value < EPOCH_FLOOR:
        raise SourceFailure(
            "%s: observed_at %d predates this feeder, so it is corruption "
            "rather than a weigh-in" % (where, value))
    if value > now + MAX_CLOCK_SKEW_SECONDS:
        raise SourceFailure(
            "%s: observed_at %d is in the future (now %d) - a reading cannot "
            "have been true before it happened, and a future stamp would keep "
            "a dead source green" % (where, value, now))
    return value


def grams_to_pounds(grams, where="grams"):
    """Convert the vendor's grams to the household's pounds.

    Google Health v4 stores body weight as `weightGrams` (a double) and NagLight
    infers its 50 lb bar from the unit string `lb`, so exactly one conversion
    exists on this path and it lives here rather than inline at the call site.
    The factor is the exact international-pound definition (0.45359237 kg), not
    a rounded 2.2046, because the panel shows one decimal and a rounded factor
    drifts visibly across the range.

    THIS IS THE FUNCTION THE WHOLE "ONE REAL CALL FIRST" GATE WAS ABOUT. The
    captured body's `weightGrams` converts, through this factor, to a number the
    Owner recognises as their own weight; through a kilogram reading it would be
    ~2200x too big and through a pound reading ~2.2x too small, and only the
    second of those is caught by the plausibility band. The mutation runs for
    this block include a mutant that returns the grams unchanged and one that
    divides by 1000, and both must be red.

    Implements: LLR-006
    """
    return check_pounds(grams, where) / 453.59237


# ══════════════════════════════════════════════════════════════════════════════
# THE GOOGLE HEALTH v4 WEIGHT PARSER, written against the body captured on
# 2026-09-09 (its shape is in the module docstring; the body itself is not in
# this repo and must never be).
# ══════════════════════════════════════════════════════════════════════════════

# The response's field names, as constants, because a name typed twice is a name
# that can drift. `dataPoints` is PLURAL: earlier prose in this repo guessed
# `dataPoint` and the observed body settled it.
DATA_POINTS_KEY = "dataPoints"
NEXT_PAGE_TOKEN_KEY = "nextPageToken"
WEIGHT_MEMBER = "weight"
WEIGHT_GRAMS_KEY = "weightGrams"
SAMPLE_TIME_KEY = "sampleTime"
PHYSICAL_TIME_KEY = "physicalTime"
UTC_OFFSET_KEY = "utcOffset"
CIVIL_TIME_KEY = "civilTime"
CIVIL_DATE_KEY = "date"

# No real UTC offset is bigger than this (the extremes in use are -12h and
# +14h). A `utcOffset` outside it is corruption, and since the offset's only job
# is to decide the reading's CALENDAR DAY, a corrupt one would silently move the
# weigh-in to the wrong day - which is precisely the mistake `civil_date` exists
# to prevent. It is answered with None ("we cannot say what day this was")
# rather than with a wrong day.
MAX_UTC_OFFSET_SECONDS = 18 * 3600

# `google-duration` is a decimal number of seconds with a trailing `s`, e.g. the
# observed `-18000s`. Fractional seconds are permitted by the format and are
# dropped here: a UTC offset is a whole number of minutes, and nothing on this
# path needs sub-second precision on a timezone.
GOOGLE_DURATION_RE = re.compile(r"^(-?)(\d+)(?:\.(\d{1,9}))?s$")

# `datetime.fromisoformat` on the Python this hub ships (3.8) accepts exactly 3
# or 6 fractional digits, while RFC3339 permits any number and Google sends 6
# today. The fraction is normalised to 6 rather than the parse being left to
# depend on a vendor's formatting choice.
FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")


def parse_rfc3339_utc(text, where):
    """Return the epoch second an RFC3339 timestamp names, or raise SourceFailure.

    Contract:
      Inputs:  text: the timestamp exactly as the vendor sent it; where: str for
               the message.
      Outputs: int epoch seconds.
      Raises:  SourceFailure for a non-string, a blank, an unparseable string,
               and - deliberately - for a stamp carrying NO ZONE at all.

    A ZONELESS STAMP IS REFUSED RATHER THAN ASSUMED TO BE UTC. `physicalTime` is
    documented and observed as UTC with a `Z`, so a stamp without one is not the
    field this parser thinks it is reading; assuming UTC would silently shift the
    reading by up to a day, which is how a stale weigh-in starts rendering fresh
    (or a fresh one starts rendering as being in the future and gets refused).

    THE OFFENDING TEXT IS NEVER PUT IN THE MESSAGE, only its type. Every string
    that reaches here came off a vendor's wire, `run_cycle` prints a
    SourceFailure's message, and systemd writes that to the journal - so the
    rule for this whole parser is that a message may name a FIELD and a TYPE and
    never a VALUE.

    Implements: LLR-006
    """
    if not isinstance(text, str) or not text.strip():
        raise SourceFailure(
            "%s: %s is missing or is not a string (it is a %s)"
            % (where, PHYSICAL_TIME_KEY, type(text).__name__))
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    match = FRACTIONAL_SECONDS_RE.search(cleaned)
    if match is not None:
        digits = (match.group(1) + "000000")[:6]
        cleaned = cleaned[:match.start(1)] + digits + cleaned[match.end(1):]
    try:
        parsed = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        raise SourceFailure(
            "%s: %s is not an RFC3339 timestamp (the value is not logged: it "
            "is a vendor's bytes)" % (where, PHYSICAL_TIME_KEY))
    if parsed.tzinfo is None:
        raise SourceFailure(
            "%s: %s carries no timezone. It is documented and was observed as "
            "UTC with a `Z`; a zoneless stamp is not that field, and assuming "
            "UTC would move the weigh-in by hours."
            % (where, PHYSICAL_TIME_KEY))
    return int(parsed.timestamp())


def parse_google_duration_seconds(text):
    """A `google-duration` (`-18000s`) as whole seconds, or None.

    None means "this parser cannot say", never zero: zero is a real offset (UTC)
    and answering it for an unreadable field would place a weigh-in on the wrong
    calendar day with total confidence.

    Implements: LLR-006
    """
    if not isinstance(text, str):
        return None
    match = GOOGLE_DURATION_RE.match(text.strip())
    if match is None:
        return None
    seconds = int(match.group(2))
    if match.group(1) == "-":
        seconds = -seconds
    if abs(seconds) > MAX_UTC_OFFSET_SECONDS:
        return None
    return seconds


def civil_date_of(sample_time, observed_at, where):
    """The CALENDAR DAY the weigh-in happened on, LOCALLY, or None.

    Contract:
      Inputs:  sample_time: the `sampleTime` object; observed_at: the epoch
               second `physicalTime` named; where: str, unused in the result and
               present so a future caller's failures can be named.
      Outputs: (year, month, day) as ints, or None when the body carries neither
               a usable `civilTime.date` nor a usable `utcOffset`.
      Raises:  nothing. "We cannot say what day this was" is an ANSWER.

    THIS IS THE TRAP THE CAPTURE PROVED, AND IT IS WHY THE FUNCTION EXISTS
    BEFORE ANY CALLER DOES. The captured body's `physicalTime` is
    2026-09-09T01:24Z and its `civilTime` is 2026-09-08 20:24, because
    `utcOffset` is -18000s. The person stood on the scale on MONDAY EVENING; in
    UTC it was already TUESDAY. Anything that ever asks "did they weigh in
    today?" - the auto-check-off SN-040 gestures at - must ask THIS function and
    never `observed_at`, or it will tick off the wrong day for every evening
    weigh-in in this timezone, which is most of them.

    `civilTime` is `readOnly` in the discovery document, i.e. the server
    computes it, so it is preferred when present; the offset arithmetic is the
    fallback for a body that omits it. `tests/test_weight_feeder.py` asserts the
    two agree on the captured shape, which is what makes the fallback trustable
    rather than merely plausible.

    Implements: LLR-006
    """
    if isinstance(sample_time, dict):
        civil = sample_time.get(CIVIL_TIME_KEY)
        if isinstance(civil, dict):
            date = civil.get(CIVIL_DATE_KEY)
            if isinstance(date, dict):
                parts = [date.get("year"), date.get("month"), date.get("day")]
                if all(isinstance(part, int) and not isinstance(part, bool)
                       for part in parts):
                    return (parts[0], parts[1], parts[2])
        offset = parse_google_duration_seconds(sample_time.get(UTC_OFFSET_KEY))
        if offset is not None:
            local = datetime.fromtimestamp(observed_at + offset, timezone.utc)
            return (local.year, local.month, local.day)
    return None


class WeightReading(object):
    """ONE weigh-in, as this feeder understands it.

    `pounds` and `observed_at` are what the gauge needs. `grams`,
    `utc_offset_seconds` and `civil_date` are carried rather than discarded
    because the next thing anyone builds on this source is the weigh-in
    check-off, and it needs the CALENDAR DAY, which `observed_at` cannot give it
    (see `civil_date_of`). A parser that threw them away would look complete and
    would quietly force the next author to re-derive them from a field they had
    already been handed.
    """

    __slots__ = ("pounds", "observed_at", "grams", "utc_offset_seconds",
                 "civil_date")

    def __init__(self, pounds, observed_at, grams, utc_offset_seconds,
                 civil_date):
        self.pounds = pounds
        self.observed_at = observed_at
        self.grams = grams
        self.utc_offset_seconds = utc_offset_seconds
        self.civil_date = civil_date

    def __repr__(self):
        # NO WEIGHT AND NO STAMP IN THE REPR. A repr reaches tracebacks and
        # logs, and this object's whole content is health data about one person.
        return "WeightReading(<not logged: health data>)"


def check_vendor_grams(raw, where):
    """`weightGrams` -> plausible pounds, or SourceFailure naming no VALUE.

    Contract:
      Inputs:  raw: whatever sat under `weightGrams`; where: str for the message.
      Outputs: float pounds inside PLAUSIBLE_LB.
      Raises:  SourceFailure for a non-number, a bool, NaN, +/-inf, zero,
               negative, and for anything converting outside the band.

    IT WRAPS `grams_to_pounds` AND `check_plausible_weight_lb` RATHER THAN
    CALLING THEM DIRECTLY FOR ONE REASON: THEIR MESSAGES QUOTE THE VALUE. That
    is right for the stored-state path those two also serve, where the value is
    ours; it is wrong here, where the value is a vendor's bytes on their way to
    the journal, and where a well-formed value IS the Owner's body weight. So
    the message names the field and the type, and the number stays out of the
    log entirely.

    THE BAND APPLIES TO A READING EXACTLY AS IT APPLIES TO A GOAL. 40..1000 lb
    is not a judgement about anybody's body; it is what catches a units error
    before it becomes a confident wrong number on a wall. Note what it CANNOT
    catch, which is why the unit is never assumed anywhere in this file: grams
    misread as pounds gives ~2.2x, and for a light enough person 2.2x still
    lands inside the band.

    Implements: LLR-006
    """
    try:
        pounds = grams_to_pounds(raw, where)
    except SourceFailure:
        raise SourceFailure(
            "%s: %s is not a usable number (it is a %s). The value is not "
            "logged." % (where, WEIGHT_GRAMS_KEY, type(raw).__name__))
    try:
        return check_plausible_weight_lb(pounds, where)
    except SourceFailure:
        raise SourceFailure(
            "%s: %s converts to a weight outside the plausible band %g..%g lb, "
            "so it is a units error or corruption rather than a body weight. "
            "The value is not logged: it is health data."
            % (where, WEIGHT_GRAMS_KEY, PLAUSIBLE_LB[0], PLAUSIBLE_LB[1]))


def weight_from_data_point(point, now, where):
    """ONE element of `dataPoints` -> a WeightReading, or SourceFailure.

    Contract:
      Inputs:  point: one element, exactly as decoded; now: this cycle's clock;
               where: "google-health dataPoints[3]" or the like.
      Outputs: WeightReading.
      Raises:  SourceFailure for a non-object, for a point carrying no `weight`
               member, for a `weight` with no `weightGrams` or no `sampleTime`,
               for an unusable number, and for a `physicalTime` that is missing,
               unparseable, zoneless, before EPOCH_FLOOR or in the FUTURE.

    A POINT WITH NO `weight` MEMBER IS NORMAL, NOT BROKEN. `DataPoint` is a
    union of 43 members in the discovery document, and this route was asked for
    the `weight` data type, so a point shaped otherwise is something this parser
    does not understand rather than something that has gone wrong. It raises
    here and `parse_weight_datapoint` SKIPS it, so one odd point among several
    costs that point and not the reading.

    `name` IS NEVER READ. It holds `users/<the Owner's Google user id>/...`, and
    a field that is never read cannot be logged, stored, or posted by accident.
    `dataSource` is not read either: `recordingMethod`/`platform` are the
    vendor's provenance and nothing on the panel is entitled to them.

    Implements: SR-022, LLR-006
    """
    if not isinstance(point, dict):
        raise SourceFailure("%s: data point is not an object (it is a %s)"
                            % (where, type(point).__name__))
    if WEIGHT_MEMBER not in point:
        raise SourceFailure(
            "%s: data point carries no `%s` member. DataPoint is a union of 43 "
            "members and this one is not a weight." % (where, WEIGHT_MEMBER))
    weight = point.get(WEIGHT_MEMBER)
    if not isinstance(weight, dict):
        raise SourceFailure("%s: `%s` is not an object (it is a %s)"
                            % (where, WEIGHT_MEMBER, type(weight).__name__))
    if WEIGHT_GRAMS_KEY not in weight:
        raise SourceFailure(
            "%s: `%s` carries no `%s`. The discovery document marks it "
            "required, so a weight without one is a shape this parser does not "
            "understand." % (where, WEIGHT_MEMBER, WEIGHT_GRAMS_KEY))
    raw_grams = weight.get(WEIGHT_GRAMS_KEY)
    pounds = check_vendor_grams(raw_grams, where)
    sample = weight.get(SAMPLE_TIME_KEY)
    if not isinstance(sample, dict):
        raise SourceFailure(
            "%s: `%s` carries no `%s` object (it is a %s). Without it there is "
            "no instant at which this weight was true, and `observed_at` may "
            "not be invented from the clock."
            % (where, WEIGHT_MEMBER, SAMPLE_TIME_KEY, type(sample).__name__))
    # `check_observed_at` refuses a stamp before this feeder existed and a stamp
    # in the FUTURE. Both are refused rather than clamped: a future stamp is
    # exactly what would keep a dead source rendering green.
    observed_at = check_observed_at(
        parse_rfc3339_utc(sample.get(PHYSICAL_TIME_KEY), where), now, where)
    return WeightReading(
        pounds=pounds,
        observed_at=observed_at,
        grams=float(raw_grams),
        utc_offset_seconds=parse_google_duration_seconds(
            sample.get(UTC_OFFSET_KEY)),
        civil_date=civil_date_of(sample, observed_at, where))


def parse_weight_datapoint(payload, now, where="google-health"):
    """ONE `ListDataPointsResponse` page -> the LATEST usable WeightReading.

    Contract:
      Inputs:  payload: the decoded body, exactly as observed; now: this cycle's
               clock, so a sample time is judged against the same instant the
               gauge is; where: str for the messages.
      Outputs: WeightReading - the one with the greatest `physicalTime`.
      Raises:  NoWeightYet when the account has logged nothing (an empty or null
               `dataPoints`); SourceFailure when the body is not an object, when
               the `dataPoints` key is ABSENT entirely, when `dataPoints` is not
               a list, when no element is usable, and when the two newest points
               share an instant but disagree about the weight.

    THE LATEST IS CHOSEN BY `physicalTime`, NOT BY POSITION, AND THAT IS A
    DECISION RATHER THAN A DETAIL. The captured body held exactly one point, so
    "the first element" and "the newest" were indistinguishable in the only
    evidence anybody has. A real history returns many, the discovery document
    promises no ordering, and `[0]` would put an arbitrary past weigh-in on the
    wall at its own stamp - honest about WHEN, wrong about WHAT, and completely
    invisible.

    AN EMPTY LIST IS `NoWeightYet`, AND AN ABSENT KEY IS NOT. They look alike
    and mean opposite things. `{"dataPoints": []}` is a 200 from a working
    source saying "this account has logged no weight" - the account is new, or
    the scale has never synced - which is the UNAVAILABLE GAUGE case: we do not
    know what you weigh, and 0 lb is not the answer. A body with no `dataPoints`
    key AT ALL is a shape that is not the one that was captured: the response
    changed, or something that is not Google answered, and reading "no weight
    logged" out of that would be reading meaning into a body we do not
    recognise.

    A BAD POINT COSTS ITSELF, NOT THE READING. Each element is parsed
    independently and its failure is collected; only if NOTHING survives does
    the page fail, and then the message carries every reason. One malformed
    entry in a synced history must not blank the panel.

    TWO NEWEST POINTS AT THE SAME INSTANT WITH DIFFERENT WEIGHTS ARE REFUSED,
    not ranked. It is the rule this module applies to two files declaring a
    goal, two items with one id and a target declared twice, and it applies for
    the same reason: whichever this code picked, somebody would be looking at a
    bar drawn around a number the other reading contradicts. Identical weights
    at one instant are a duplicate, not a contradiction, and are accepted.

    Implements: SR-022, LLR-006
    """
    if not isinstance(payload, dict):
        raise SourceFailure("%s: the response body is not an object (it is a %s)"
                            % (where, type(payload).__name__))
    if DATA_POINTS_KEY not in payload:
        raise SourceFailure(
            "%s: the response body carries no `%s` key at all. The body "
            "captured on 2026-09-09 had one, so this is a shape this parser "
            "does not recognise rather than an empty history - it is refused "
            "instead of read as 'no weight logged'." % (where, DATA_POINTS_KEY))
    points = payload.get(DATA_POINTS_KEY)
    if points is None or (isinstance(points, list) and not points):
        raise NoWeightYet(
            "%s: HTTP 200 with no data points, so this account has no weight "
            "logged in Google Health yet. That is not a broken source and not "
            "a reading of 0 - it is the unavailable gauge: we do not know what "
            "you weigh. Step on a scale that syncs to Google Health."
            % where)
    if not isinstance(points, list):
        raise SourceFailure("%s: `%s` is not a list (it is a %s)"
                            % (where, DATA_POINTS_KEY, type(points).__name__))
    readings, problems = [], []
    for index, point in enumerate(points):
        try:
            readings.append(weight_from_data_point(
                point, now, "%s %s[%d]" % (where, DATA_POINTS_KEY, index)))
        except SourceFailure as exc:
            problems.append(str(exc))
    if not readings:
        raise SourceFailure(
            "%s: %d data point(s) and not one usable weight among them (%s)"
            % (where, len(points), "; ".join(problems)))
    newest = max(reading.observed_at for reading in readings)
    tied = [reading for reading in readings if reading.observed_at == newest]
    if len({reading.grams for reading in tied}) > 1:
        raise SourceFailure(
            "%s: %d data points share the newest instant and disagree about "
            "the weight. Which one the household weighs is not guessable, and "
            "picking one would draw a bar around a number the other "
            "contradicts." % (where, len(tied)))
    return tied[0]


def next_page_token(payload):
    """The `nextPageToken` of a list response, or None when there is not one.

    A non-string, or an empty string, is None: the field's whole meaning is "ask
    again with this", and there is nothing to ask again with.

    Implements: LLR-006
    """
    if not isinstance(payload, dict):
        return None
    token = payload.get(NEXT_PAGE_TOKEN_KEY)
    return token if isinstance(token, str) and token.strip() else None


def clean_scalar(text):
    """Strip the quoting and the trailing `# comment` a hand-edited YAML scalar
    may carry, and return what the person meant.

    Definitions frontmatter is edited by hand in a spreadsheet cell and in a
    text editor, so `170`, `"170"`, `'170'` and `170 # summer` are all the same
    number, and `lb`, `"lb"` and `lb  # pounds` are all the same unit.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith(("'", '"')) and len(cleaned) >= 2 and cleaned[-1] == cleaned[0]:
        return cleaned[1:-1].strip()
    return cleaned.split("#", 1)[0].strip()


def parse_goal_pounds(text, where, key):
    """Parse a declared goal, in pounds. The caller has ALREADY checked the unit.

    Contract:
      Inputs:  text: the raw scalar exactly as written after `<key>:`;
               where: str for the message; key: the field's name, so the
               message names the thing the person actually typed.
      Outputs: float in GOAL_MIN_LB..GOAL_MAX_LB.
      Raises:  GoalMissing for a blank; ValueError for anything unparseable or
               out of band, naming the file so the person can fix their own
               definitions.

    Implements: LLR-006
    """
    cleaned = clean_scalar(text)
    if not cleaned:
        raise GoalMissing("%s: %s is present but blank" % (where, key))
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError("%s: %s is not a number: %r" % (where, key, text))
    if not math.isfinite(value):
        raise ValueError("%s: %s is not finite: %r" % (where, key, text))
    if value < GOAL_MIN_LB or value > GOAL_MAX_LB:
        raise ValueError(
            "%s: %s is %g lb, outside the sanity band %g..%g. This is a typo "
            "guard, not a judgement: a goal one digit out drags the inferred "
            "50 lb bar off the scale and paints every real reading full red."
            % (where, key, value, GOAL_MIN_LB, GOAL_MAX_LB))
    return value


# One `key: value` line, with its own indentation captured. The key pattern is
# deliberately narrow so a prose line that happens to contain a colon is not
# read as a field.
FIELD_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s?(.*)$")

# The one key whose sequence this reader follows into.
ITEMS_KEY = "items"


def frontmatter_lines(body):
    """The lines BETWEEN the first two `---` fences, or None if there are none.

    Matches internal/defs.frontmatter: the prose body of a definitions file is
    free notes, and a `target:` written down there is someone thinking out loud,
    not declaring anything.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None                       # not a definitions file at all
    out = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        out.append(line)
    return out


def leading_indent(raw, where, number):
    """The width of `raw`'s leading whitespace, refusing a TAB outright.

    YAML FORBIDS TABS IN INDENTATION - not as a style rule, as a parse error:
    `yaml.v3`, which is what NagLight's own internal/defs loads these files
    with, rejects the document. A reader that quietly accepted a tab would be
    reading a file the real loader cannot read, and would then be the ONLY
    thing in the household that believes it knows what that file declares. So
    a tab in the indentation is a refusal that names the line, not a width.
    """
    lead = raw[:len(raw) - len(raw.lstrip())]
    if "\t" in lead:
        raise ValueError(
            "%s: line %d is indented with a TAB. YAML forbids tabs in "
            "indentation, so the loader that syncs these files rejects the "
            "whole document - this feeder will not be the one thing that "
            "thinks it knows what the file says. Indent it with spaces."
            % (where, number))
    return len(raw) - len(raw.lstrip(" "))


def parse_definitions_file(body, where="the definitions file"):
    """Split ONE definitions file's frontmatter into its top-level keys and its
    items, in the shape NagLight's internal/defs would see.

    Contract:
      Inputs:  body: the whole .md file's text; where: its path, for messages.
      Outputs: (top: {key: [raw value, ...]}, items: [{field: [raw value, ...]}])
               or None when the text is not a definitions file.
      Raises:  ValueError when the frontmatter cannot be read HONESTLY - a tab
               indent, a second `items:`, or an inline `items:` sequence.

    Values are kept as LISTS of the raw text so a duplicate declaration is
    visible to the caller rather than silently resolved by "last one wins" -
    picking one of two contradictory goals is the defect this module refuses
    everywhere else.

    INDENTATION DEPTH IS THE STRUCTURE, AND IGNORING IT WAS THE CROSS-REVIEW
    DEFECT. The first cut read any indented `key: value` inside `items:` as a
    field of the current item, at whatever depth it sat. Four separate findings
    fell out of that one mistake, and every one ends with a plausible,
    confident, WRONG number about the Owner's body on the wall:

        items:                          items:
          - id: weigh-in                  - id: take-vitamins
            metadata:                       alternatives:
              target: 170                     - id: weigh-in
              unit: lb                          target: 170
                                                unit: lb

    Neither of those declares a 170 lb goal to any YAML parser alive: the first
    is `metadata.target`, the second is a nested list belonging to a DIFFERENT
    item. Both used to yield 170. So did a second `items:` block lower down,
    and so did a tab-indented block that yaml.v3 refuses to parse at all.

    The fix is one rule, not four patches: A FIELD BELONGS TO AN ITEM ONLY AT
    THAT ITEM'S OWN FIELD COLUMN. The sequence's indent is fixed by its first
    `- ` entry; the item's field column is fixed by the first field on that
    entry; a line deeper than the field column is the content of a nested
    container and is NOT the item's, and a `- ` deeper than the sequence indent
    is a nested list's entry and is NOT an item.

    AND AMBIGUITY IS REFUSED, NOT RESOLVED. A second `items:` key is not a
    continuation to be merged and not a reset to be preferred; which sequence
    the household meant is not guessable, so the document is refused. That is
    the same rule this module already applies to two files declaring the goal,
    two items with one id, and a target declared twice.

    WHY THIS IS STILL A HAND READER AND NOT PyYAML. The service is stdlib-only
    by design - a plain unit under ProtectSystem=strict with no venv - so a real
    YAML parser means a new apt package name and the offline apt export re-run
    that goes with it. It would also be the WRONG SHAPE: a full parser is
    maximally permissive, and anchors, aliases and merge keys can make a goal
    arrive from a line the person cannot see beside the number. What this reader
    owes the household is the opposite - to read the narrow block subset the
    sheet actually generates and REFUSE everything else rather than interpret
    it. `tests/test_weight_feeder.py` checks this reading against PyYAML as an
    oracle wherever PyYAML happens to be installed, so "narrow" cannot quietly
    become "different".

    TOP-LEVEL KEYS STOP AT `items:`, exactly as internal/defs does. A key at
    column zero AFTER the item sequence is malformed YAML that the frontmatter
    happens to contain, not a declaration, and reading it would let a stray
    line at the bottom of a hand-edited file speak for the household.

    Implements: LLR-006
    """
    lines = frontmatter_lines(body)
    if lines is None:
        return None
    top, items = {}, []
    in_items = after_items = False
    seq_indent = field_indent = current = None
    for offset, raw in enumerate(lines):
        number = offset + 2               # +1 for the fence, +1 for 1-based
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = leading_indent(raw, where, number)
        text = raw.strip()

        # Have we fallen out of the item sequence?
        if in_items and seq_indent is not None and indent < seq_indent:
            in_items, seq_indent, field_indent, current = False, None, None, None
        elif in_items and seq_indent is None and not text.startswith("-"):
            # `items:` was declared and the next thing is not a sequence entry.
            in_items = False

        # Column zero, or anywhere outside the sequence.
        if not in_items:
            if indent:
                continue                  # indented, but inside nothing we read
            match = FIELD_RE.match(text)
            if match is None:
                continue
            key = match.group(2)
            if key == ITEMS_KEY:
                if after_items:
                    raise ValueError(
                        "%s declares `%s:` twice in one frontmatter (line %d). "
                        "Which sequence carries the household's items is not "
                        "guessable, and merging them would let a block nobody "
                        "is looking at supply a number. Delete one."
                        % (where, ITEMS_KEY, number))
                inline = clean_scalar(match.group(3))
                if inline not in ("", "[]"):
                    raise ValueError(
                        "%s writes `%s:` as an inline sequence (line %d). This "
                        "reader follows the block form the sheet generates and "
                        "refuses to guess at any other, because guessing wrong "
                        "here posts a number about somebody's body."
                        % (where, ITEMS_KEY, number))
                in_items, after_items = True, True
                seq_indent = field_indent = current = None
                continue
            if after_items:
                continue                  # see the docstring: not a declaration
            top.setdefault(key, []).append(match.group(3))
            continue

        # Inside the item sequence.
        if text.startswith("-"):
            if seq_indent is None:
                seq_indent = indent       # the first entry fixes the sequence
            if indent != seq_indent:
                continue                  # a NESTED list's entry, not an item
            current = {}
            items.append(current)
            rest = text[1:]
            column = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            rest = rest.strip()
            field_indent = column if rest else None
            if not rest:
                continue                  # `-` alone: the fields are below it
            match = FIELD_RE.match(rest)
            if match is not None:
                current.setdefault(match.group(2), []).append(match.group(3))
            continue
        if current is None:
            continue                      # an indented line before any `- `
        if field_indent is None:
            if indent <= seq_indent:
                continue
            field_indent = indent         # the first field fixes the column
        if indent != field_indent:
            continue                      # NOT this item's: nested, or ragged
        match = FIELD_RE.match(text)
        if match is None:
            continue
        current.setdefault(match.group(2), []).append(match.group(3))
    return top, items


def one_value(values, where, what):
    """The single raw value for a field, or ValueError if it was declared twice.

    Two contradictory declarations of the same thing are not a tie to be broken:
    whichever this code picked, the person would be looking at a bar drawn
    around a number they thought they had changed.
    """
    if len(values) > 1:
        raise ValueError(
            "%s declares %s %d times; which one is authoritative is not "
            "guessable." % (where, what, len(values)))
    return values[0]


def find_goal_item(body, where, category, item_id):
    """The item this household's goal lives in, from ONE definitions file.

    Contract:
      Inputs:  body: the file's text; where: its path; category and item_id:
               the LOCATION knobs, already resolved.
      Outputs: {field: [raw value, ...]} for the matching item, or None when
               this file is not that category or holds no such item.
      Raises:  ValueError when the file declares its category twice, or holds
               the same item id twice - defs.Load found duplicate ids on a real
               household's files, and the loser is never used and never
               reported.

    THE MATCH IS CASE-INSENSITIVE ON BOTH HALVES. Both strings are typed by a
    person into a spreadsheet cell, and `Health` against `health` must not be
    the difference between a goal and a dark panel.

    Implements: SR-022, LLR-006
    """
    parsed = parse_definitions_file(body, where)
    if parsed is None:
        return None
    top, items = parsed
    if "category" not in top:
        return None
    declared = clean_scalar(one_value(top["category"], where, "category"))
    if declared.casefold() != category.strip().casefold():
        return None
    wanted = item_id.strip().casefold()
    matches = [
        item for item in items
        if "id" in item
        and clean_scalar(one_value(item["id"], where, "an item id")).casefold() == wanted]
    if len(matches) > 1:
        raise ValueError(
            "%s holds %d items with id %r under category %r; which one carries "
            "the goal is not guessable." % (where, len(matches), item_id, category))
    return matches[0] if matches else None


def goal_from_item(item, where, category, item_id):
    """Turn the matched item's `target`/`unit` into the goal, in pounds.

    Contract:
      Outputs: float pounds.
      Raises:  GoalMissing when there is no target, or it is blank - the
               person has not said what they are aiming at, and there is no
               honest bar to draw. ValueError when a target IS declared but
               cannot be honoured: unusable as a number, or carrying no unit,
               or carrying any unit but `lb`. The line is "nothing declared is
               missing; something declared that we cannot use is a refusal
               that names the file".

    THE UNIT IS CHECKED BEFORE THE NUMBER IS EVEN PARSED, AND THAT ORDER IS THE
    POINT. `target: 77` with `unit: kg` is 170 lb, and 77 sits comfortably
    INSIDE the pounds sanity band - so the band would not catch it, and the
    panel would show "77 lb" against a real reading of 191 and paint it full
    red. There is deliberately NO CONVERSION: a silent kg->lb conversion is the
    same bet as a vendor parser written from a schema, and this feeder refuses
    that bet everywhere else. If the household ever wants kilograms, that is a
    requirement change with its own tests, not a factor dropped in here.

    Implements: SR-022, LLR-006
    """
    where_item = "%s (category %s, item %s)" % (where, category, item_id)
    if TARGET_KEY not in item:
        raise GoalMissing(
            "%s declares no `%s:`. The goal is that item's target, so with no "
            "target there is no goal and no honest bar to draw."
            % (where_item, TARGET_KEY))
    raw_target = one_value(item[TARGET_KEY], where_item, TARGET_KEY)
    if not clean_scalar(raw_target):
        raise GoalMissing("%s: %s is present but blank" % (where_item, TARGET_KEY))
    if UNIT_KEY not in item:
        raise ValueError(
            "%s declares `%s: %s` but no `%s:`. The unit is not assumed: a "
            "target read in the wrong unit posts a confident, plausible, wrong "
            "number about someone's body. Add `%s: %s`."
            % (where_item, TARGET_KEY, clean_scalar(raw_target), UNIT_KEY,
               UNIT_KEY, GOAL_UNIT))
    declared_unit = clean_scalar(one_value(item[UNIT_KEY], where_item, UNIT_KEY))
    if declared_unit.casefold() != GOAL_UNIT:
        raise ValueError(
            "%s declares `%s: %s`, and this feeder reads %s only. It does NOT "
            "convert: %s in another unit would be posted as %s against a %s "
            "reading, which is a wrong number that looks entirely believable."
            % (where_item, UNIT_KEY, declared_unit or "(blank)", GOAL_UNIT,
               clean_scalar(raw_target), GOAL_UNIT, GOAL_UNIT))
    return parse_goal_pounds(raw_target, where_item, TARGET_KEY)


def declares_legacy_goal(body, where="the definitions file"):
    """True when this file still carries the superseded top-level goal key.

    Only its PRESENCE is read, never its value: the key is not the goal any
    more, and parsing it would be the first step back towards honouring it.

    Implements: LLR-006
    """
    parsed = parse_definitions_file(body, where)
    return parsed is not None and LEGACY_GOAL_KEY in parsed[0]


def gauge_body(value_lb, goal_lb, observed_at):
    """Assemble the POST body in the exact shape /api/feed accepts.

    Contract:
      Inputs:  value_lb: float pounds, finite and positive;
               goal_lb:  float pounds - the TARGET LINE, from the definitions;
               observed_at: epoch seconds when the weight was TRUE, or None.
      Outputs: dict ready to json-encode.
      Raises:  ValueError if this repo would emit a body the server must 400.

    FOUR RULES ARE ENFORCED HERE RATHER THAN TRUSTED:
      * `value` and `target` are ALWAYS present. An omitted value is a 400 now
        and used to be stored as a fabricated 0 - and a 0 lb body weight is the
        single worst number this file could invent.
      * NO `min`/`max`: `lb` is the one unit NagLight infers a range for, and
        the range rule stays in the tracker (GAUGE_MIN_MAX_OMITTED).
      * NO `window` and therefore NO `direction`: a goal is a standing line,
        and NagLight refuses a direction without a window.
      * NO colour, NO severity, NO `css`. NagLight derives them; a feeder that
        computed a hue would make the panel's authority ambiguous.
      * A BODY THAT CARRIES `observed_at` MUST CARRY A PLAUSIBLE WEIGHT. Only
        one body this feeder builds is allowed the sentinel 0, and it is
        exactly the one with NO stamp - the never-measured gauge. Any other 0,
        `-500` or `100000` is a units error or a corrupted state file, and
        finiteness alone did not catch either.

    Implements: SR-022, LLR-006
    """
    for name, number in (("value", value_lb), ("target", goal_lb)):
        if isinstance(number, bool) or not isinstance(number, (int, float)) \
                or not math.isfinite(float(number)):
            raise ValueError("gauge %s: %s is not a finite number: %r"
                             % (GAUGE_ID, name, number))
    if observed_at is not None:
        try:
            check_plausible_weight_lb(value_lb, "gauge value")
        except SourceFailure as exc:
            raise ValueError("gauge %s: %s" % (GAUGE_ID, exc))
    for name, text in (("id", GAUGE_ID), ("label", GAUGE_LABEL),
                       ("icon", GAUGE_ICON), ("unit", GAUGE_UNIT)):
        if len(text) > RUNE_LIMIT:
            raise ValueError("gauge %s: %s exceeds %d runes" % (GAUGE_ID, name, RUNE_LIMIT))
    body = {
        "kind": "gauge",
        "id": GAUGE_ID,
        "label": GAUGE_LABEL,
        "icon": GAUGE_ICON,
        "unit": GAUGE_UNIT,
        "value": float(value_lb),
        "target": float(goal_lb),
    }
    if observed_at is not None:
        body["observed_at"] = iso8601_utc(int(observed_at))
    return body


def iso8601_utc(epoch_seconds):
    """Epoch seconds -> the RFC3339 `Z` form NagLight parses.

    The gauge contract takes `observed_at` as an RFC3339 STRING (NagLight
    parses it with time.RFC3339 and serves a gauge it cannot parse as stale),
    which is where this feeder's wire shape parts company with the usage
    feeder's integer stamps. Getting this wrong does not fail loudly - it
    renders as a permanently stale gauge - so it is one function with one test.

    Implements: LLR-006
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch_seconds)))


def validate_stored_reading(entry, now):
    """Return a stored reading only if it is credible history, else None.

    Contract:
      Inputs:  entry: whatever `load_state` found under the gauge's key - any
               JSON value at all, including None; now: this cycle's clock.
      Outputs: {"value": float lb, "observed_at": int}, or None.
      Raises:  nothing. An unusable entry is an ANSWER, not an exception: the
               caller's job then is identical to a source that never succeeded.

    THE STATE FILE IS INPUT, NOT MEMORY. It is a file on disk that a
    half-written cycle, a disk error or a person with an editor can change, and
    `build_post` treats what it holds as a weight that was once TRUE - it is
    reposted at its own stamp and the panel draws a 50 lb bar around it.
    Nothing used to check it (`gauge_body` asked only whether the number was
    finite), so a corrupted entry could put `-500 lb` on the wall, or carry a
    stamp in the FUTURE and make a source that has been dead for a week render
    live. A nonsensical stored reading is not history, it is a failure, and it
    gets the answer a failed source with no history gets: value 0, no stamp,
    "unavailable".

    Implements: SR-022, LLR-006
    """
    if not isinstance(entry, dict):
        return None
    try:
        value = check_plausible_weight_lb(entry.get("value"), "stored value")
        stamp = check_observed_at(entry.get("observed_at"), now, "stored reading")
    except SourceFailure:
        return None
    return {"value": value, "observed_at": stamp}


def build_post(reading, last, goal_lb, now):
    """Decide what to POST this cycle - the "stale, never green" rule.

    Contract:
      Inputs:  reading: (value_lb, observed_at) if THIS cycle read the source
                        successfully, else None - every SourceFailure class
                        collapses to None;
               last:    the stored {"value", "observed_at"} from a previous
                        successful cycle, or None if there has never been one;
               goal_lb: the target line from the user's definitions;
               now:     epoch seconds.
      Outputs: (body: dict, fresh: bool). `fresh` is True only when the body
               carries a stamp from THIS cycle's read.
      Raises:  ValueError for a body this repo should never build, and
               SourceFailure when THIS cycle's reading is itself not credible -
               an implausible weight or a stamp in the future. Both are caught
               per-gauge by `run_cycle` and become the unavailable gauge, so a
               bad reading costs the reading and never the post.

    THE INVARIANT, and it is half the acceptance criterion in one line:
        the body carries a stamp from this cycle IF AND ONLY IF reading is not
        None.
    Everything else follows from NagLight's own rule that `observed_at` is when
    the value was TRUE:
      * source read           -> the real weight at the time it was measured;
      * source failed, but a previous reading exists -> that weight, stamped
        WHEN IT WAS TRUE. It is not a lie, and it goes stale on the `static`
        7-day horizon, so the panel shows "unavailable" rather than a stale
        green bar sitting on last month's number;
      * source failed and there has never been a reading -> value 0 with NO
        `observed_at` at all, which NagLight treats as stale on arrival. The
        gauge EXISTS so the panel can say "unavailable" instead of showing
        nothing where a bar belongs, and the 0 is unreachable as a DISPLAYED
        reading by construction. This is the one number in this file that was
        not measured, and it is exactly the number the wire contract was
        tightened over - which is why it can only ever appear together with a
        missing stamp.

    NOTE that the freshness of the SOURCE, not of the POST, is what matters. A
    reading whose `observed_at` is a fortnight old is posted at that stamp and
    NagLight renders it stale, because it IS stale - the person has not weighed
    themselves. A feeder that re-stamped it to `now` to keep the bar green
    would be the exact fabrication this design forbids, and it is the mistake
    that is easiest to make while "fixing" a gauge that says unavailable.

    Implements: SR-022, LLR-006
    """
    if reading is not None:
        # Either a WeightReading (what the vendor reader returns now, because
        # the check-off needs its day facts) or the bare pair.
        value_lb, observed_at = reading_pair(reading)
        # The SOURCE's own numbers are checked here as well as in the reader,
        # because this is the point at which they become a gauge: a stamp in
        # the future or a weight outside the plausible band must not be posted
        # however it got this far.
        return gauge_body(check_plausible_weight_lb(value_lb, "reading"),
                          goal_lb,
                          check_observed_at(observed_at, now, "reading")), True
    # Re-validated at the point of USE as well as at the point of load: the two
    # are separated by the whole source read, and the guard has to hold at both.
    checked = validate_stored_reading(last, now)
    if checked is not None:
        return gauge_body(checked["value"], goal_lb, checked["observed_at"]), False
    return gauge_body(0.0, goal_lb, None), False


def is_fresh(body, now):
    """True if NagLight would still consider this body's reading current.

    Used by the tests to assert "stale, never green" directly against
    NagLight's horizon table rather than against our own intent: a body with no
    `observed_at`, one that does not parse, one older than the horizon for its
    window kind, or one stamped in the future is not fresh. This gauge has no
    window, so the horizon is the `static` one.

    Implements: LLR-006
    """
    stamp = body.get("observed_at")
    if stamp is None:
        return False
    if isinstance(stamp, str):
        try:
            parsed = time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False               # unparseable is stale, per SR-067
        import calendar
        stamp = calendar.timegm(parsed)
    if stamp > now:
        return False                   # a future stamp is stale by NagLight's rule
    kind = body.get("window", {}).get("kind")
    return (now - stamp) <= STALE_HORIZON_SECONDS.get(kind, 24 * 3600)


def reading_pair(reading):
    """(pounds, observed_at) from a WeightReading OR from a bare pair.

    `read_google_health` returns the whole WeightReading now, because the
    check-off needs the CALENDAR DAY and the vendor must be read exactly ONCE
    per cycle - re-reading it to recover a field we already had would double
    the calls that carry the access token. The gauge half still wants only the
    two numbers, and a test (or a future second source) may hand back the plain
    pair the readers used to return, so both shapes are accepted here rather
    than at four call sites.

    Implements: LLR-006
    """
    if isinstance(reading, WeightReading):
        return reading.pounds, reading.observed_at
    return reading[0], reading[1]


def reading_day_facts(reading):
    """(civil_date, utc_offset_seconds) from a reading, or (None, None).

    A bare pair carries no day facts at all, and "we cannot say what day this
    was" is an ANSWER here exactly as it is in `civil_date_of`: the caller's
    job is then to NOT tick.

    Implements: LLR-006
    """
    if isinstance(reading, WeightReading):
        return reading.civil_date, reading.utc_offset_seconds
    return None, None


def local_civil_date(epoch_seconds, utc_offset_seconds):
    """(year, month, day) at `epoch_seconds` as seen from that UTC offset.

    The offset comes from the READING - Google reports the one the scale's
    phone was standing in - so this needs no TZ database, no $TZ on the hub,
    and no guess about where the household is. It is the same arithmetic
    `civil_date_of` uses for its fallback, factored out so "what day was the
    weigh-in" and "what day is it now" are computed by ONE function and cannot
    drift apart by a rounding or a sign.

    Implements: LLR-006
    """
    local = datetime.fromtimestamp(int(epoch_seconds) + int(utc_offset_seconds),
                                   timezone.utc)
    return (local.year, local.month, local.day)


def should_check_off(reading, last_checked, now):
    """Whether to POST the automated tick this cycle. Four gates, all required.

    Contract:
      Inputs:  reading: what THIS cycle read - a WeightReading, a bare pair, or
               None when the source failed; last_checked: the epoch sample time
               already ticked for this check id, or None; now: this cycle's
               clock.
      Outputs: bool.
      Raises:  nothing. Every "we cannot tell" is a False.

    THE GATE THAT MATTERS MOST IS THE FIRST ONE, AND IT IS THE WHOLE REASON
    THIS IS A FUNCTION RATHER THAN AN `if` IN `run_cycle`. When the source
    fails, `build_post` re-posts the LAST REAL VALUE AT ITS ORIGINAL STAMP so
    the gauge stays honest and goes stale on its own. That path must NEVER
    tick: a tick is a claim that a person stood on a scale, and a source that
    is down is the one circumstance in which this feeder knows nothing about
    whether they did. `reading is None` covers every SourceFailure class, the
    unexpected-exception class, and the refused-gauge fallback, because
    `run_cycle` clears `reading` there too.

      1. A GENUINELY FRESH READ. `reading is not None` - not "the cycle
         worked", not "the post succeeded", not `fresh` from `build_post`
         (which is the same flag, but reading it here would make the tick
         depend on a gauge decision that is free to change).
      2. A SAMPLE TIME WE HAVE NOT TICKED. Strictly newer than `last_checked`.
         The source keeps returning the same weigh-in for the whole week; the
         feeder wakes every 15 minutes. Without this the item would be
         re-ticked 96 times a day, each one a `SaveDay`, a panel wake and (on
         a single-user box) a git commit, for a fact that has not changed.
      3. A DAY WE CAN NAME. `civil_date` is not None and the reading carries a
         `utcOffset` - without both, "did this happen today?" is unanswerable
         and the honest answer is to stay quiet.
      4. TODAY, IN THE READING'S OWN LOCAL FRAME. The legacy lane CANNOT
         back-date (see `check_body`): NagLight stamps the tick on ITS today
         and ignores `at` entirely on the `ok` path. So a three-day-old
         weigh-in, recovered when the feeder or the network comes back, must
         not be ticked - it would put "weighed in" on a day nobody weighed in
         on. Missing a weigh-in is recoverable by a tap on the panel; asserting
         one that did not happen is not.

    WHAT THIS DELIBERATELY DOES NOT DO IS POST `ok: false`. There is no
    circumstance in which this feeder knows someone did NOT weigh in, and
    un-ticking would silently erase a tick the Owner made by hand.

    Implements: SR-022, LLR-006
    """
    if reading is None:
        return False
    _pounds, observed_at = reading_pair(reading)
    if last_checked is not None and observed_at <= last_checked:
        return False
    civil_date, offset = reading_day_facts(reading)
    if civil_date is None or offset is None:
        return False
    return civil_date == local_civil_date(now, offset)


def check_body(check_id):
    """The SECOND body: the legacy-lane tick for an `type: automated` item.

    Contract:
      Inputs:  check_id: the item's `check:` value, non-blank.
      Outputs: dict ready to json-encode.
      Raises:  ValueError for a blank id, which NagLight answers 400
               "missing check id".

    THREE ABSENCES ARE THE CONTRACT, and each of them is a way to get this
    wrong (all read off NagLight's `handleAPIFeed`):

      * NO `kind`. `kind` is the GAUGE lane's selector; `case "":` is what
        routes a body to the boolean/colour/rgb struct. A `kind` here would be
        either "unknown feed kind" (400) or, worse, the gauge again.
      * NO `color` and NO `rgb`. The handler counts signals among
        `ok != nil`, `color != ""`, `rgb != ""` and refuses anything but
        EXACTLY ONE. `ok` is a *bool in Go, so its PRESENCE - not its value -
        is what selects this lane; `false` is a signal too, which is why
        `should_check_off` returns a decision to post or not post rather than
        a value to post.
      * NO `note`, AND NO `at`.
          - `note` because the gauge already carries the number and a note is
            the obvious place a body weight would leak into the tracker's log
            lines, which are mirrored to a private repo hourly (SN-024). It
            would also be dead weight: the handler decodes `Note` and then
            never reads it - grep it - so nothing would ever display it.
          - `at` because on the `ok` path it is IGNORED. Only the colour/rgb
            path parses it (as RFC3339, into logfile.Report); the boolean path
            goes straight to `date := s.Now()`. Sending one would look like
            back-dating works and quietly wouldn't. See `should_check_off`
            gate 4 and stack/weight/README.md.

    Implements: SR-022, LLR-006
    """
    if not isinstance(check_id, str) or not check_id.strip():
        raise ValueError("check id is blank; NagLight answers 400 to that")
    return {"check": check_id.strip(), "ok": True}


def cycle_now(env):
    """The single instant this cycle reasons about, in epoch seconds.

    `run_cycle` puts it in the environment alongside `_identity` and
    `_feed_url` so the reader - and the parser that will one day sit behind it
    - measures a sample time against the SAME `now` the gauge is judged by.

    Implements: LLR-006
    """
    raw = env.get("_now")
    return int(raw) if raw is not None else int(time.time())


def resolve_state_root(env):
    """The ONE directory under which this feeder may write, whatever the config says.

    Contract:
      Config:  STATE_DIRECTORY - exported by systemd for a unit carrying
               `StateDirectory=`, which this one does
               (`StateDirectory=homehub-weight`).
      Outputs: an absolute path; DEFAULT_STATE_ROOT when the variable is absent.

    WHY THIS EXISTS SEPARATELY FROM WEIGHT_STATE_FILE. The allow-list in
    `open_for_write` is built from a path the CONFIG names, so on its own it
    proves only that the feeder wrote where it was told - it cannot tell a
    state file from the Owner's refresh token if the knob names the token.
    Binding the write to the service's OWN state directory makes the guard
    independent of the .env, and StateDirectoryMode=0700 backs it from the
    other side.

    Implements: SR-022, LLR-006
    """
    return (env.get("STATE_DIRECTORY") or "").strip() or DEFAULT_STATE_ROOT


def resolve_enabled(env):
    """True only for a literal `true`. Off is the default and every typo.

    Gated exactly like REMOTE_UI_ENABLED, AI_CLI_ENABLED and AI_USAGE_ENABLED:
    an unset knob, a blank one, `True`, `yes` and `1` are all OFF, because a
    feeder that starts on an ambiguous value starts on a box nobody asked.

    Implements: SR-022, LLR-006
    """
    return (env.get("WEIGHT_ENABLED") or "false").strip() == "true"


def resolve_identity(env):
    """Return the ONE household identity this gauge is posted for.

    Contract:
      Config:  WEIGHT_USER - the stable Google `sub`, exactly as
               NAGLIGHT_USER / TRACKER_DRIVE_USER / AI_USAGE_USER are.
      Raises:  SystemExit when it is unset or blank.

    IT REFUSES TO GUESS, and erroring out is the refusal - not a default. This
    matters more here than for a usage gauge: a body weight on the wrong
    household member's board is not merely wrong, it is a private measurement
    shown to the wrong person on a wall in a shared room.

    Implements: SR-022, LLR-006
    """
    value = (env.get("WEIGHT_USER") or "").strip()
    if not value:
        raise SystemExit(
            "REFUSED: WEIGHT_USER is unset. This feeder posts for ONE explicit "
            "identity (the stable Google `sub`, the same value as NAGLIGHT_USER) "
            "and will not guess whose body weight this is. Set it in stack/.env, "
            "or set WEIGHT_ENABLED=false.")
    return value


def is_local_destination(host, bridge_addresses=None):
    """True for loopback or an address a LOCAL docker bridge really carries.

    `bridge_addresses` is injectable so the tests can state the box's bridges
    rather than depending on whatever this machine happens to have. A hostname
    is refused outright rather than resolved: resolution is a moving target and
    `localhost.example.com` is a real trick.

    Implements: LLR-006
    """
    import ipaddress
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if bridge_addresses is None:
        bridge_addresses = local_bridge_networks()
    for network in bridge_addresses:
        if addr in network:
            return True
    return False


def resolve_feed_url(env):
    """Return the /api/feed URL, refusing a destination off this box.

    Contract:
      Config:  WEIGHT_FEED_URL - required, no default.
      Raises:  SystemExit when unset, when the scheme is not http/https, or
               when the host is not loopback and not an address a local docker
               bridge is carrying.

    Same reasoning as the usage feeder's, and one degree stronger. The POST
    carries the tracker bearer token, the household identity in
    `X-Forwarded-User`, AND a person's body weight. The tracker is bridge-only
    by design (no host publish), so the only legitimate destinations are its
    loopback and its bridge address; the public https route would bounce
    through oauth2-proxy and OVERWRITE the identity header, and any other host
    would hand a household member's weight to a stranger.

    Implements: SR-022, LLR-006
    """
    raw = (env.get("WEIGHT_FEED_URL") or "").strip()
    if not raw:
        raise SystemExit(
            "REFUSED: WEIGHT_FEED_URL is unset. There is no default: the "
            "tracker is bridge-only, so the right value depends on this box.")
    from urllib.parse import urlsplit
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise SystemExit("REFUSED: WEIGHT_FEED_URL scheme %r is not http/https"
                         % parts.scheme)
    host = parts.hostname or ""
    if not host:
        raise SystemExit("REFUSED: WEIGHT_FEED_URL has no host")
    if not is_local_destination(host):
        raise SystemExit(
            "REFUSED: WEIGHT_FEED_URL host %r is neither loopback nor an "
            "address a local docker bridge is carrying. The POST carries the "
            "tracker token, the household identity and a person's body "
            "weight; it must not leave this box." % host)
    return raw


# ── SHELL: everything below touches the world ───────────────────────────────

def local_bridge_networks():
    """The networks the box's docker bridges are actually carrying.

    Reads `ip -json addr` and keeps only `docker0` and `br-*`. An empty list is
    a legitimate answer (a box with no bridge) and means only loopback is an
    acceptable destination - the safe direction to fail.
    """
    import ipaddress
    import subprocess
    out = []
    try:
        raw = subprocess.run(["ip", "-json", "addr"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return out
    if raw.returncode != 0:
        return out
    try:
        links = json.loads(raw.stdout)
    except ValueError:
        return out
    for link in links if isinstance(links, list) else []:
        name = link.get("ifname", "")
        if name != "docker0" and not name.startswith("br-"):
            continue
        for info in link.get("addr_info", []) or []:
            address, prefix = info.get("local"), info.get("prefixlen")
            if not address or prefix is None:
                continue
            try:
                out.append(ipaddress.ip_network("%s/%s" % (address, prefix), strict=False))
            except ValueError:
                continue
    return out


def writable_path_verdict(path, state_path, state_root, resolve=None):
    """Decide whether `path` may be written. Returns a refusal string, or None.

    Contract:
      Inputs:  path: the file about to be opened for writing;
               state_path: the one file this feeder owns (WEIGHT_STATE_FILE);
               state_root: the directory this service's StateDirectory= gave it;
               resolve: the path resolver - `os.path.realpath` in production,
                 injected only so a test can state what a symlink resolves to
                 on a filesystem that will not let it create one.
      Outputs: None if the write is allowed, else the reason it is refused.
      Raises:  nothing. It DECIDES; `open_for_write` is what refuses.

    WHY realpath AND NOT abspath - THE CROSS-REVIEW DEFECT. `os.path.abspath`
    is string arithmetic and does NOT resolve symlinks, so pre-creating
    `<state>.tmp` as a link pointing at the Owner's Google refresh token passed
    the old allow-list unchanged (the STRING matched) and `save_state` opened
    it "w" and truncated it. A refresh token minted at a browser does not
    survive that: it costs a person a trip back to a browser. The shape of the
    allow-list was right; the resolution was wrong.

    THE STATE ROOT IS A SECOND, INDEPENDENT BOUND. The allow-list is derived
    from a path the CONFIG names, so it can only say "the feeder wrote where it
    was told". Requiring the resolved path to sit inside the service's own
    StateDirectory refuses a .env that names the token too.

    This is B7's function with this feeder's names. It is duplicated rather
    than shared because the two feeders ship as standalone scripts under
    separate units, accounts and ProtectSystem=strict roots, with no importable
    module between them; `tests/test_feeder_egress_parity.py` is what keeps the
    two copies honest.

    Implements: SR-022, LLR-006
    """
    resolve = resolve or os.path.realpath
    # The basename is read off BOTH the name we were handed and the file it
    # really resolves to: the first names the hazard when someone points the
    # state knob at a token, the second when a link does the pointing.
    for named in (path, resolve(path)):
        if os.path.basename(named) in CREDENTIAL_BASENAMES:
            return ("%s (which resolves to %s) is a credential file. This "
                    "feeder reads credentials and never writes one."
                    % (path, named))
    root = resolve(state_root)
    inside = os.path.join(root, "")
    real_state = resolve(state_path)
    resolved = resolve(path)
    # ONE containment check, on the path actually about to be OPENED. The first
    # cut also checked `state_path` separately, which looked like defence in
    # depth and was the same check twice: the allow-list below pins `resolved`
    # to `real_state` or its `.tmp` sibling, so a state file outside the root
    # can only ever reach the open as one of those two and is caught here. The
    # mutation run proved the redundancy by deleting either copy and staying
    # green, and a guard that only the other guard proves is a guard nobody has
    # tested.
    if not (resolved == root or resolved.startswith(inside)):
        return ("%s resolves to %s, which is outside this service's own state "
                "directory %s. A state path that leaves StateDirectory= is a "
                "knob pointing at something that is not state."
                % (path, resolved, root))
    if resolved not in (real_state, real_state + ".tmp"):
        return ("%s resolves to %s, which is outside the one path this feeder "
                "may write (%s)." % (path, resolved, real_state))
    return None


def open_for_write(path, state_path, state_root):
    """The ONLY way this feeder opens a file for writing. THE token guard.

    Contract:
      Inputs:  path/state_path/state_root as `writable_path_verdict` takes them.
      Outputs: an open text-mode handle.
      Raises:  PermissionError for anything the verdict refuses, and OSError
               from the kernel if the final component is a symlink or if the
               temp file appeared between the verdict and the open.

    WHY AN ALLOW-LIST AND NOT A DENY-LIST - B7's shape, kept because the
    reasoning is not usage-specific. "Never writes a credential file" cannot be
    met by listing the files we know about; the next vendor tool puts its token
    somewhere new. So the feeder declares the ONE path it may write and refuses
    the rest of the filesystem, and CREDENTIAL_BASENAMES is a second check that
    exists only to make the message name the real hazard.

    THE GOOGLE HEALTH REFRESH TOKEN IS READ-ONLY TO THIS PROCESS and is not on
    the allow-list, and after this round it cannot be reached through a link on
    the allow-list either.

    Implements: SR-022, LLR-006
    """
    reason = writable_path_verdict(path, state_path, state_root)
    if reason:
        raise PermissionError("REFUSED: " + reason)
    return open_no_follow(path, root=state_root)


def path_components_under(path, root):
    """The component names leading from `root` down to `path`.

    Contract:
      Inputs:  path: the file about to be written; root: the directory the
               write must stay inside.
      Outputs: [name, ...] - at least one - naming each hop from `root`.
      Raises:  ValueError when `path` is not literally under `root`, which is
               the only case where this function cannot make the containment
               claim and so refuses to make it.

    LITERAL, NOT RESOLVED, AND ON PURPOSE. Resolution is what the verdict
    already did; what the OPEN needs is the sequence of names it will actually
    walk, so that each hop can be checked as it is taken. The deployed layout
    is literal (`/var/lib/homehub-weight` from `StateDirectory=`), so a path
    that is only under the root by way of a symlink is a shape this feeder does
    not need and will not silently accept.
    """
    root_abs = os.path.abspath(root)
    head, parts = os.path.abspath(path), []
    while head != root_abs:
        head, tail = os.path.split(head)
        if not tail:
            raise ValueError("%s is not under %s" % (path, root))
        parts.insert(0, tail)
    if not parts:
        raise ValueError("%s IS %s, not a file inside it" % (path, root))
    return parts


# `openat`-style relative opens exist on Linux (the hub) and not on Windows
# (the dev PC). Which one is in force decides which guard below is doing the
# work, and the tests assert the behaviour rather than the mechanism.
DIR_FD_OPENS = (os.open in getattr(os, "supports_dir_fd", set())
                and hasattr(os, "O_DIRECTORY"))


def open_no_follow(path, root=None):
    """Open `path` for writing, following NO symlink at ANY component.

    Contract:
      Inputs:  path: the file to create; root: the directory the walk starts
               from and may not leave - the service's own state root.
      Outputs: an open text-mode handle, 0600.
      Raises:  PermissionError when a component would leave `root`; OSError
               from the kernel for a symlinked component, or for anything that
               appeared between the verdict and the open.

    THE CHECK-THEN-OPEN RACE IS THE POINT. `writable_path_verdict` resolves the
    path, but anything planted between that resolution and the open would be
    followed by the open. So the file is unlinked first - which destroys a
    planted LINK and never the file it points at, and clears a `.tmp` left by a
    killed cycle - then created with O_CREAT|O_EXCL so the kernel refuses
    anything that appeared in the gap.

    O_NOFOLLOW ALONE WAS NOT ENOUGH, AND THAT IS THIS ROUND'S FIX. It protects
    the FINAL component only. Replace an intermediate directory after the
    verdict has resolved - swap `.../tokens` for a link to somebody's home
    directory - and the old open walked through it happily: every guard above
    it had already run, and the file landed outside the state directory the
    module's own docstring says it cannot leave. A containment claim that holds
    only for the last hop is not a containment claim.

    So the walk is now taken ONE COMPONENT AT A TIME from `root`: each
    intermediate directory is opened relative to the previous one with
    O_DIRECTORY|O_NOFOLLOW, so a component that has become a link is refused by
    the kernel AT THE HOP, and the final create happens relative to a directory
    handle rather than to a name that can be re-pointed under it.

    ON WINDOWS THERE ARE NO `dir_fd` OPENS, so the dev PC falls back to
    checking each component with `lstat` before the open. That check is
    check-then-use and does not close the race - it is not claimed to. The hub
    is Linux, where the guard above is whole; the fallback exists so the same
    tests exercise the same refusals here.

    `root=None` keeps the old single-component behaviour for a caller that has
    no root to be contained in. Nothing in this repo passes None.
    """
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if root is None:
        try:
            os.unlink(path)
        except OSError:
            pass
        return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")

    try:
        parts = path_components_under(path, root)
    except ValueError as exc:
        raise PermissionError(
            "REFUSED: %s cannot be opened as a path inside %s (%s), so this "
            "write cannot be contained and is not attempted."
            % (path, root, exc))

    if not DIR_FD_OPENS:                  # pragma: no cover - Windows dev PC
        walked = os.path.abspath(root)
        for name in parts[:-1]:
            walked = os.path.join(walked, name)
            if os.path.islink(walked):
                raise PermissionError(
                    "REFUSED: %s is a symlink, so writing %s would land "
                    "outside the state directory %s."
                    % (walked, path, root))
        try:
            os.unlink(path)
        except OSError:
            pass
        return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")

    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            hop = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=parent)
            os.close(parent)
            parent = hop
        leaf = parts[-1]
        try:
            os.unlink(leaf, dir_fd=parent)
        except OSError:
            pass
        handle = os.fdopen(os.open(leaf, flags, 0o600, dir_fd=parent), "w",
                           encoding="utf-8")
    finally:
        os.close(parent)
    return handle


# Second-line names only;
# Second-line names only; `open_for_write`'s allow-list is the real guard.
CREDENTIAL_BASENAMES = frozenset({
    "google-health-token.json",   # what WEIGHT_TOKEN_FILE normally is
    "credentials.json",
    ".credentials.json",
    "auth.json",
    ".netrc",
})


def validate_checked_marks(entry, now):
    """The `checked` block of the state file, VALIDATED. Anything else drops.

    Contract:
      Inputs:  entry: whatever sat under CHECK_STATE_KEY - any JSON value;
               now: this cycle's clock.
      Outputs: {check id: int epoch seconds}, possibly empty.
      Raises:  nothing.

    A MARK THAT DOES NOT VALIDATE IS DROPPED, AND DROPPING IT IS SAFE ONLY
    BECAUSE `should_check_off` HAS A SECOND, INDEPENDENT GATE. Losing the mark
    means "we have not ticked anything for this id", which on its own would
    let an old weigh-in be ticked again on the wrong day - the exact lie this
    block exists to avoid. Gate 4 (the weigh-in must have happened TODAY in its
    own local frame) is what makes that harmless: a lost mark can at worst
    re-tick a reading from today, onto today, on an item that is already done.
    The two gates are deliberately not derived from each other.

    `check_observed_at` is reused rather than re-implemented, so a mark in the
    FUTURE - the one that would suppress every real tick until the clock caught
    up - is refused by the same rule that refuses a future reading.

    Implements: LLR-006
    """
    if not isinstance(entry, dict):
        return {}
    out = {}
    for key, value in entry.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            out[key] = check_observed_at(value, now, "checked mark %r" % key)
        except SourceFailure:
            continue
    return out


def load_state(state_path, now):
    """Last successful reading, VALIDATED. Anything else is {}.

    A missing or unparseable file is {} as before, and so now is an entry
    `validate_stored_reading` will not vouch for - because what this file holds
    is reposted to the wall as a body weight, and a state file is input rather
    than memory.

    CHECK_STATE_KEY IS THE ONE KEY THAT IS NOT A READING, and it gets its own
    validator rather than an exemption: without the special case the generic
    loop below would silently DROP the tick marks on every load, the feeder
    would believe it had never ticked, and gate 2 of `should_check_off` would
    stop working - which is the sort of failure that shows up as 96 identical
    ticks a day rather than as an error.
    """
    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, entry in data.items():
        if key == CHECK_STATE_KEY:
            marks = validate_checked_marks(entry, now)
            if marks:
                out[key] = marks
            continue
        checked = validate_stored_reading(entry, now)
        if checked is not None:
            out[key] = checked
    return out


def save_state(state, state_path, state_root):
    """Write the state file through `open_for_write`, atomically.

    The state holds A WEIGHT, A TIMESTAMP, AND THE CHECK IDS ALREADY TICKED
    WITH THE SAMPLE TIME EACH WAS TICKED FOR - never a token, never a header,
    never a response body. That is asserted by test, because a state file that
    quietly grew a `refresh_token` would turn this feeder into the
    credential-writing thing it promises not to be.

    THE TICK MARKS SHARE THIS FILE RATHER THAN GETTING ONE OF THEIR OWN, and
    that is a security decision, not tidiness: `open_for_write` allows exactly
    one path plus its `.tmp`, realpath-resolved and contained in the
    StateDirectory, opened O_NOFOLLOW. A second state file would mean a second
    blessed path, and the allow-list is the guard.
    """
    tmp = state_path + ".tmp"
    with open_for_write(tmp, state_path, state_root) as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    # os.replace does NOT follow a symlink at the destination - it replaces the
    # link itself - so the rename cannot reach the token either, and the
    # verdict has already refused a state path resolving outside the root.
    os.replace(tmp, state_path)


def resolve_goal_location(env):
    """Which item carries the goal, as (category, item_id).

    Contract:
      Outputs: (str, str) - never blank; a blank or absent knob takes the
               default, because the default IS the shape the Owner's synced
               sheet already has.

    THESE ARE LOCATION KNOBS, NOT A GOAL KNOB, AND THE DIFFERENCE IS THE WHOLE
    ACCEPTANCE CRITERION. `WEIGHT_ITEM_CATEGORY` and `WEIGHT_ITEM_ID` name a
    place in the person's own definitions; neither can hold a weight. So the
    household's intent still lives in exactly one place, still syncs from the
    person's phone, and still cannot be set on the hub - a test scans
    `.env.example` and `FieldSchema.psd1` for any goal-shaped name and fails on
    one. They are knobs at all so that renaming the item or moving it to
    another category is an .env edit, not a code change.

    Implements: SR-022, LLR-006
    """
    category = (env.get("WEIGHT_ITEM_CATEGORY") or "").strip() or DEFAULT_GOAL_CATEGORY
    item_id = (env.get("WEIGHT_ITEM_ID") or "").strip() or DEFAULT_GOAL_ITEM_ID
    return category, item_id


def scan_definitions(defs_dir, category, item_id):
    """Walk the definitions ONCE and return ((item, path) | None, [legacy paths]).

    Contract:
      Inputs:  defs_dir: the user's `definitions/`; category and item_id: the
               resolved LOCATION knobs.
      Outputs: (found, legacy) - `found` is the matched item's field map with
               the file it came from, or None; `legacy` lists every file still
               carrying the superseded top-level key.
      Raises:  GoalMissing when the directory is not there; ValueError for a
               file that links OUT of the directory, a file that cannot be
               read, and the item appearing in two files.

    THIS IS THE WALK `load_goal_from_definitions` ALWAYS DID, lifted out
    unchanged so the check-off can ask the SAME question of the SAME files
    without a second copy of the containment rule. Two copies of a `realpath`
    containment check is how one of them ends up subtly weaker.

    Implements: SR-022, LLR-006
    """
    if not os.path.isdir(defs_dir):
        raise GoalMissing(
            "no definitions directory at %s. The goal is the `%s:` of the `%s` "
            "item under category `%s` in the person's own definitions, so with "
            "no definitions there is no goal and no honest bar to draw."
            % (defs_dir, TARGET_KEY, item_id, category))
    names = sorted(n for n in os.listdir(defs_dir) if n.endswith(".md"))
    real_dir = os.path.realpath(defs_dir)
    found, legacy = None, []
    for name in names:
        path = os.path.join(defs_dir, name)
        # A DEFINITIONS FILE THAT IS A LINK OUT OF THE DIRECTORY IS REFUSED,
        # not read. The directory is the tracker's own docker volume and so is
        # inside the trust boundary - WEIGHT_DEFINITIONS_DIR may itself be a
        # link, which is why the comparison is made against its RESOLVED form
        # rather than its name - but "the goal came from a file that is not in
        # the household's definitions at all" is a sentence this feeder should
        # never be able to say, and one realpath is what it costs to make sure.
        if os.path.dirname(os.path.realpath(path)) != real_dir:
            raise ValueError(
                "definitions file %s resolves to %s, outside the definitions "
                "directory %s. The goal is read from the household's own "
                "definitions, so a file that links out of them is refused "
                "rather than read." % (path, os.path.realpath(path), real_dir))
        try:
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
        except OSError as exc:
            raise ValueError("cannot read definitions file %s: %s"
                             % (path, type(exc).__name__))
        if declares_legacy_goal(body, path):
            legacy.append(path)
        item = find_goal_item(body, path, category, item_id)
        if item is None:
            continue
        if found is not None:
            raise ValueError(
                "item `%s` under category `%s` appears in both %s and %s; "
                "which one carries the goal is not guessable, and picking the "
                "first would silently follow file-name order."
                % (item_id, category, found[1], path))
        found = (item, path)
    return found, legacy


def check_id_from_definitions(defs_dir, category=None, item_id=None):
    """The feeder check id to tick, or None when the item does not want one.

    Contract:
      Inputs:  the same directory and location knobs the goal is read from.
      Outputs: str - the item's `check:` - when that item declares BOTH
               `type: automated` and a non-blank `check:`; None otherwise.
      Raises:  nothing. Every "no" - no directory, no item, no `type`, a `type`
               that is not `automated`, no `check`, a `check` declared twice,
               an unreadable file - is None.

    IT ANSWERS None RATHER THAN RAISING BECAUSE THE TICK IS THE OPTIONAL HALF.
    The gauge is the thing SN-040 promises: the panel must say what the person
    weighs, or say it does not know. The tick is an extra, and an extra must
    never be able to take the gauge down with it - so this function's failure
    mode is silence, and `run_cycle` calls it only AFTER the gauge is posted.
    The refusals that DO matter (no goal, no source) already have their own
    loud paths and are unchanged.

    `type: automated` IS CHECKED AS WELL AS `check:`, even though NagLight
    checks it too. NagLight's answer to a `check:` on a `type: habit` item is
    400 "unknown feeder check id" - correct, but it arrives once per fresh
    reading as a journal line the Owner would have to decode. Reading the type
    here means reverting the item to `habit` simply stops the tick, quietly,
    which is what "the definitions are the declaration" has to mean if it means
    anything.

    Implements: SR-022, LLR-006
    """
    category = category or DEFAULT_GOAL_CATEGORY
    item_id = item_id or DEFAULT_GOAL_ITEM_ID
    try:
        found, _legacy = scan_definitions(defs_dir, category, item_id)
    except (GoalMissing, ValueError, OSError):
        return None
    if found is None:
        return None
    item, where = found
    if TYPE_KEY not in item or CHECK_KEY not in item:
        return None
    try:
        declared_type = clean_scalar(one_value(item[TYPE_KEY], where, TYPE_KEY))
        declared_check = clean_scalar(one_value(item[CHECK_KEY], where, CHECK_KEY))
    except ValueError:
        # Declared twice: which one is authoritative is not guessable, and this
        # half stays quiet rather than picking one. The goal loader raises on
        # the same shape for `target`, which is the loud half.
        return None
    if declared_type.casefold() != AUTOMATED_TYPE:
        return None
    return declared_check or None


def load_goal_from_definitions(defs_dir, category=None, item_id=None):
    """Read the goal out of the user's OWN definitions. Half the acceptance.

    Contract:
      Inputs:  defs_dir: the directory holding this user's definition files -
               the same `definitions/` NagLight loads with internal/defs.Load
               and keeps in step with Drive; category and item_id: which item
               carries it (see `resolve_goal_location`).
      Outputs: (goal_lb: float, source_file: str).
      Raises:  GoalMissing when the directory is absent, holds no *.md, holds
               no such item, or that item declares no target. ValueError when
               the declaration cannot be honoured - a bad target, a unit that
               is not `lb`, two files carrying the item, or the superseded
               top-level key still being present.

    WHY THE DEFINITIONS AND NOT A KNOB ON THE HUB. SN-040 says "the goal lives
    in the user's definitions so it syncs like everything else", and that is a
    behaviour statement, not a filing preference. Definitions are the one thing
    in this system a person edits from a phone and that arrives on the hub by
    itself. A goal in stack/.env would need an SSH session and a redeploy to
    change, would not travel with the rest of the person's tracker, and would
    be a second place the household's intent lives - which is how this repo's
    own /opt/homehub drift started. That has NOT changed. What changed is WHICH
    KEY inside the definitions carries it, and why is at the top of this module.

    WHY AN ITEM'S `target` AND NOT A TOP-LEVEL KEY - THE SHORT VERSION, BECAUSE
    A FUTURE READER WILL BE TEMPTED TO PUT IT BACK. This household runs Drive
    SHEET mode, where internal/defsheet regenerates every .md from a fixed
    ITEM-column list and drops what it does not recognise, so a top-level
    `weight_goal_lb` is erased by the first sync after any sheet edit. `target`
    and `unit` are already item columns that round-trip. And there is no vendor
    fallback to lean on: Google Health v4 has no goal concept anywhere in its
    discovery document.

    BOTH PRESENT IS A REFUSAL, NOT A PRECEDENCE. If a file still carries the
    superseded `weight_goal_lb` AND the item carries a `target`, this raises
    rather than picking one. Silent precedence is the trap: whichever way it
    fell, the person would be looking at a bar drawn around one number while a
    different number sat in their file looking authoritative - and in sheet
    mode the top-level one is about to be deleted underneath them, so "the
    newest edit wins" is not even stable. It is the same rule this module
    already applies to two files declaring a goal, for the same reason: which
    one is authoritative is not guessable, so it is asked rather than assumed.

    Implements: SR-022, LLR-006
    """
    found, legacy = scan_definitions(defs_dir, category or DEFAULT_GOAL_CATEGORY,
                                     item_id or DEFAULT_GOAL_ITEM_ID)
    category = category or DEFAULT_GOAL_CATEGORY
    item_id = item_id or DEFAULT_GOAL_ITEM_ID
    # WHETHER THE ITEM ACTUALLY CARRIES A TARGET, not merely whether the item
    # exists, decides which refusal the person gets. An item with no target
    # beside a lingering `weight_goal_lb` is a HALF-DONE MIGRATION, and telling
    # that person "both are present, delete one" would be advice that leaves
    # them with no goal at all.
    has_target = (found is not None and TARGET_KEY in found[0]
                  and bool(clean_scalar(found[0][TARGET_KEY][0])))
    if has_target and legacy:
        raise ValueError(
            "%s still declares the superseded top-level `%s:` while %s carries "
            "the goal as the `%s` item's `%s:`. This is REFUSED rather than "
            "resolved by precedence: two numbers both look authoritative, and "
            "in Drive sheet mode the top-level one is deleted by the next sync "
            "anyway. Delete the `%s:` line and the goal is unambiguous."
            % (legacy[0], LEGACY_GOAL_KEY, found[1], item_id, TARGET_KEY,
               LEGACY_GOAL_KEY))
    if not has_target and legacy:
        raise ValueError(
            "%s declares the top-level `%s:`, which is NO LONGER READ. Drive "
            "sheet mode regenerates definitions from the item columns and "
            "drops unknown top-level keys, so that line would be erased by the "
            "next sync. Move the number onto the `%s` item under category `%s` "
            "as `%s: <lb>` with `%s: %s`, and delete the `%s:` line."
            % (legacy[0], LEGACY_GOAL_KEY, item_id, category, TARGET_KEY,
               UNIT_KEY, GOAL_UNIT, LEGACY_GOAL_KEY))
    if found is None:
        raise GoalMissing(
            "no definitions file under %s holds an item with id `%s` under "
            "category `%s`. That item's `%s:` IS the goal, so add it (with "
            "`%s: %s`), or point WEIGHT_ITEM_CATEGORY / WEIGHT_ITEM_ID at the "
            "item you keep it on. It is NOT a hub setting on purpose: it "
            "belongs to the person, beside the things they track, so it syncs "
            "with them." % (defs_dir, item_id, category, TARGET_KEY, UNIT_KEY,
                            GOAL_UNIT))
    return goal_from_item(found[0], found[1], category, item_id), found[1]


# ── The verified Google Health v4 facts, as constants rather than prose ─────
# Every one of these was read off the live discovery document (revision
# 20260907) or a live 401, on 2026-09-09. They are constants so that the
# eventual implementation cannot drift from what was actually verified, and so
# a test can assert the scope is the shared metrics one rather than an invented
# weight-specific string.
GOOGLE_HEALTH_DISCOVERY = "https://health.googleapis.com/$discovery/rest?version=v4"
GOOGLE_HEALTH_LIST_URL = (
    "https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints")
GOOGLE_HEALTH_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly")
GOOGLE_HEALTH_FILTER = 'weight.sample_time.physical_time >= "%s"'
GOOGLE_HEALTH_RPC = (
    "google.devicesandservices.health.v4.DataPointsService.ListDataPoints")

# Google's OAuth token endpoint. The feeder does its own refresh-token grant -
# it cannot import `weight_oauth`, which imports THIS module - so the constant
# lives here and `weight_oauth.TOKEN_ENDPOINT` is asserted equal to it by test,
# the same way `tests/test_feeder_egress_parity.py` keeps the two feeders'
# duplicated guards honest.
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# THE OAUTH CLIENT RESOLUTION ORDER, WHICH IS `weight_oauth`'s AND MUST STAY
# `weight_oauth`'s. `OAUTH2_PROXY_*` first because that is the household's ONE
# Google OAuth client (the live hub's .env was listed by key name on 2026-09-09
# and carries no TRACKER_DRIVE_CLIENT_* pair); the tracker pair second, for a
# differently-provisioned box. FIRST COMPLETE PAIR WINS, and a pair is complete
# only when BOTH halves are set - half a pair is a mis-provisioned box, and
# sliding to the next one would send Google an id from one place and a secret
# from another, which fails as `invalid_client` and looks like Google's fault.
# A test asserts this tuple equals `weight_oauth.CLIENT_KEY_PAIRS`, so the token
# is refreshed with the client it was minted by.
OAUTH_CLIENT_KEY_PAIRS = (
    ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET"),
    ("TRACKER_DRIVE_CLIENT_ID", "TRACKER_DRIVE_CLIENT_SECRET"),
)

# How many list pages this feeder will walk before it refuses. See
# `list_weight_data_points` for the reasoning and for which half of it is an
# assumption.
MAX_LIST_PAGES = 20


def resolve_oauth_client(env):
    """The (client_id, client_secret) this refresh is made with.

    Contract:
      Config:  the pairs in OAUTH_CLIENT_KEY_PAIRS, in order, from the unit's
               EnvironmentFile=/opt/homehub/stack/.env.
      Raises:  SourceFailure naming EVERY variable looked for and whether each
               is set - and NEVER a value.

    IT IS A SourceFailure AND NOT A SystemExit, unlike the identity and
    destination refusals. Those two are configuration this box cannot run
    without at all; a missing OAuth client means one source cannot be read this
    cycle, which is the unavailable gauge - the panel says "we do not know what
    you weigh" and the goal, the post and the state file all still work.

    Implements: SR-022, LLR-006
    """
    for id_key, secret_key in OAUTH_CLIENT_KEY_PAIRS:
        client_id = (env.get(id_key) or "").strip()
        client_secret = (env.get(secret_key) or "").strip()
        if client_id and client_secret:
            return client_id, client_secret
    halves = ["%s (%s)" % (key, "set" if (env.get(key) or "").strip()
                           else "unset or blank")
              for pair in OAUTH_CLIENT_KEY_PAIRS for key in pair]
    raise SourceFailure(
        "google-health: no complete Google OAuth client pair in this unit's "
        "environment, so the refresh token cannot be exchanged. Looked for, in "
        "order: %s. A pair counts only when BOTH halves are set. The unit gets "
        "these from EnvironmentFile=/opt/homehub/stack/.env; no value is "
        "logged here." % "; ".join(halves))


def read_refresh_token(path):
    """The refresh token out of WEIGHT_TOKEN_FILE. READ ONLY, never written.

    Every failure is a SourceFailure naming the errand, because each one has a
    different answer and "unavailable" alone would not say which: no file means
    `weight_oauth.py mint` has not been run on this box, and a file with no
    `refresh_token` means the mint half-succeeded and wants `--force`.

    THE TOKEN IS NEVER IN A MESSAGE - not on success, not on failure. The PATH
    is, because a path is configuration the Owner typed and is what tells them
    which box or which knob is wrong.

    Implements: SR-022, LLR-006
    """
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except OSError as exc:
        raise SourceFailure(
            "google-health: cannot read the token file %s (%s). Run "
            "`weight_oauth.py mint` on this box - see stack/weight/README.md."
            % (path, type(exc).__name__))
    except ValueError:
        raise SourceFailure(
            "google-health: the token file %s is not JSON. Run "
            "`weight_oauth.py mint --force`." % path)
    token = stored.get("refresh_token") if isinstance(stored, dict) else None
    if not isinstance(token, str) or not token:
        raise SourceFailure(
            "google-health: the token file %s carries no `refresh_token`. Run "
            "`weight_oauth.py mint --force`." % path)
    return token


def vendor_json(request, timeout, what):
    """Send `request` through `vendor_opener()` and decode the JSON. One door.

    Contract:
      Outputs: the decoded body.
      Raises:  SourceFailure for EVERY failure class - transport, timeout,
               refused redirect, non-200 and an undecodable body.

    NOTHING THE REMOTE WROTE EVER REACHES THE MESSAGE. Not the body, not on a
    401, not on a 500. `run_cycle` prints a SourceFailure's message and systemd
    writes it to the journal, and a Google error body has been observed
    quoting the offending request back - which on this path carries an
    `Authorization` header that is the whole health-metrics scope. The status
    code is ours to read; the body is the remote's to write, and it does not
    get a journal. `EgressRefused` is converted here, deliberately and by name,
    because its message is OURS.

    Implements: SR-022, LLR-006
    """
    try:
        with vendor_opener().open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SourceFailure("google-health: %s answered HTTP %s"
                                    % (what, response.status))
            raw = response.read()
    except SourceFailure:
        raise
    except EgressRefused as exc:
        raise SourceFailure("google-health: %s was refused: %s" % (what, exc))
    except urllib.error.HTTPError as exc:
        raise SourceFailure(
            "google-health: %s failed: HTTP %s (the body is not logged). 401 "
            "means the refresh token is no longer valid - run `weight_oauth.py "
            "mint --force`; 403 usually means the scope is no longer on the "
            "consent screen." % (what, exc.code))
    except Exception as exc:
        raise SourceFailure("google-health: %s failed: %s"
                            % (what, type(exc).__name__))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SourceFailure("google-health: %s returned something that is not "
                            "JSON" % what)


def google_access_token(client_id, client_secret, refresh_token, timeout,
                        endpoint=None):
    """Exchange the stored refresh token for a short-lived access token.

    NOT OBSERVED FROM THIS PROCESS. `weight_oauth.py capture` performs the same
    grant and its call is what the Owner ran; this is the same request made by
    the feeder. If it is wrong the outcome is a named SourceFailure and an
    unavailable gauge, never a reading.

    Implements: SR-022, LLR-006
    """
    fields = {"client_id": client_id, "client_secret": client_secret,
              "refresh_token": refresh_token, "grant_type": "refresh_token"}
    request = urllib.request.Request(
        endpoint or GOOGLE_TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST")
    payload = vendor_json(request, timeout, "the token refresh")
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SourceFailure(
            "google-health: the token refresh returned no access token. If the "
            "refresh token has been revoked, run `weight_oauth.py mint "
            "--force`.")
    return token


def list_weight_data_points(access_token, now, timeout, list_url=None):
    """Walk the `dataPoints` list and return the LATEST reading across it.

    Contract:
      Outputs: WeightReading.
      Raises:  NoWeightYet when every page was empty; SourceFailure for a
               transport failure, an unrecognised body, and for a history
               deeper than MAX_LIST_PAGES pages.

    NO QUERY PARAMETERS ARE SENT ON THE FIRST CALL, AND THAT IS THE OBSERVATION
    SPEAKING. The one call anybody has ever made against this route sent none
    and returned 200. The discovery document also offers a `filter`
    (GOOGLE_HEALTH_FILTER, still unused below) and a `pageSize`, and either
    would usefully bound the walk - but adding an unexercised query parameter to
    the single request shape that is KNOWN to work is precisely the bet this
    block's gate exists to refuse. When somebody runs the filtered call by hand
    and sees a 200, it can be added here with the same ceremony.

    WHAT HAPPENS ON A `nextPageToken` IS AN ASSUMPTION, MARKED AS ONE. The
    captured body had none, so nothing about paging has been observed. The
    reasoning: the acceptance criterion is the LATEST reading, the response
    promises no ordering, and pages left unwalked could hold a newer weigh-in
    than any seen - so a page token is FOLLOWED, up to MAX_LIST_PAGES, and a
    history still not exhausted after that is REFUSED rather than answered from
    the part that was seen. Refusing costs nothing the household will feel: the
    failure path re-posts the last real reading at its ORIGINAL stamp, so the
    panel degrades to the previous truth instead of going dark, and 20 pages of
    weigh-ins is a depth a household does not reach. Answering from a partial
    walk would mean claiming "latest" for a reading this code cannot know is
    the latest, and this file does not make claims it cannot support.

    AN EMPTY PAGE MID-WALK IS NOT AN EMPTY HISTORY. `NoWeightYet` from one page
    is swallowed and the walk continues; it is only raised to the caller if
    NOTHING was found anywhere.

    Implements: SR-022, LLR-006
    """
    base = list_url or GOOGLE_HEALTH_LIST_URL
    headers = {"Authorization": "Bearer " + access_token,
               "Accept": "application/json"}
    latest, token, pages = None, None, 0
    while True:
        url = base if token is None else (
            base + ("&" if "?" in base else "?")
            + urllib.parse.urlencode({"pageToken": token}))
        payload = vendor_json(urllib.request.Request(url, headers=headers),
                              timeout, "the dataPoints list call")
        pages += 1
        try:
            reading = parse_weight_datapoint(payload, now)
        except NoWeightYet:
            reading = None
        if reading is not None and (latest is None
                                    or reading.observed_at > latest.observed_at):
            latest = reading
        token = next_page_token(payload)
        if token is None:
            break
        if pages >= MAX_LIST_PAGES:
            raise SourceFailure(
                "google-health: the history is still not exhausted after %d "
                "pages, so this cycle cannot know which weigh-in is the latest "
                "and will not claim one. No reading was taken and no number "
                "was invented." % pages)
    if latest is None:
        raise NoWeightYet(
            "google-health: HTTP 200 across %d page(s) with no data points, so "
            "this account has no weight logged in Google Health yet. That is "
            "not a broken source and not a reading of 0 - it is the "
            "unavailable gauge: we do not know what you weigh." % pages)
    return latest


def read_google_health(env, list_url=None, token_endpoint=None):
    """Source: Google Health API v4, Weight data type. THE ONE VENDOR READ.

    Contract:
      Inputs:  env: the process environment plus `_now`; the two endpoints are
               injected ONLY by tests, exactly as `weight_oauth` injects them -
               there is no knob for either, because a knob on the URL that
               carries this token is a way to send it somewhere else.
      Outputs: the WeightReading itself.
      Raises:  SourceFailure for every failure class, NoWeightYet (a subclass)
               for an account with nothing logged. Both take the unavailable
               path in `build_post`; neither can produce a reading.

    IT ORCHESTRATES AND DOES NOT INTERPRET: token file, OAuth client, access
    token, list walk, one reading.

    IT USED TO RETURN ONLY `(pounds, observed_at)` - the two numbers the gauge
    needs - on the reasoning that a caller wanting the CALENDAR DAY would call
    `parse_weight_datapoint` itself. The check-off is that caller, and it lives
    in `run_cycle`, which reaches the vendor only through this function. Making
    it call the parser as well would mean a SECOND list request carrying the
    access token every 15 minutes, to recover a field this call already had. So
    the whole reading comes back and `reading_pair` takes the two numbers off
    it for the gauge. What has NOT changed is that a day must never be
    re-derived from `observed_at`: that is UTC, and it is a day out for every
    evening weigh-in in this timezone. See `civil_date_of`.

    Implements: SR-022, LLR-006
    """
    token_file = (env.get("WEIGHT_TOKEN_FILE") or "").strip()
    if not token_file:
        raise SourceFailure(
            "google-health: WEIGHT_TOKEN_FILE is unset, so there is no refresh "
            "token to read and no reading was taken. Run `weight_oauth.py "
            "mint` - see stack/weight/README.md.")
    refresh_token = read_refresh_token(os.path.expanduser(token_file))
    client_id, client_secret = resolve_oauth_client(env)
    try:
        timeout = int(env.get("WEIGHT_TIMEOUT_SECONDS") or 30)
    except ValueError:
        raise SourceFailure(
            "google-health: WEIGHT_TIMEOUT_SECONDS is not a whole number of "
            "seconds.")
    access_token = google_access_token(client_id, client_secret, refresh_token,
                                       timeout, token_endpoint)
    return list_weight_data_points(access_token, cycle_now(env), timeout,
                                   list_url)


SOURCE_READERS = {
    "google-health": read_google_health,
}


def enabled_source(env):
    """Which source to read. Unknown names are REFUSED rather than skipped."""
    name = (env.get("WEIGHT_SOURCE") or "google-health").strip()
    if name not in SOURCE_READERS:
        raise SystemExit("REFUSED: WEIGHT_SOURCE names an unknown source: %r "
                         "(known: %s)" % (name, ", ".join(sorted(SOURCE_READERS))))
    return name


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every 3xx instead of re-sending the request to `Location`.

    HALF ONE OF THE EGRESS DEFECT, and it matters more here than in the usage
    feeder: urllib's default opener follows redirects and copies the original
    request's headers onto the new request, so the validated loopback endpoint
    could answer `302 Location: http://attacker.example/feed` and urllib would
    re-send the POST - the feed bearer token, the X-Forwarded-User identity,
    and a body whose one number is THE OWNER'S BODY WEIGHT - off this box.

    NEITHER THE TARGET NOR THE RESPONSE IS PUT IN THE MESSAGE: a hostile or
    confused server chooses that string, and the message ends up in the journal.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EgressRefused(
            "refused an HTTP %s redirect: this request may not be re-sent to "
            "a destination the response chose" % code)


def _peer_checked_connection(base_class, allow_host):
    """A connection class that asks the socket where it really landed.

    The check runs inside `connect()`, after the TCP handshake and BEFORE
    http.client writes a byte of the request line, so a connection to an
    address that is not on this box is dropped with the token and the weight
    still unsent.
    """

    class PeerChecked(base_class):
        def connect(self):
            base_class.connect(self)
            try:
                peer = self.sock.getpeername()[0]
            except (OSError, AttributeError, IndexError):
                peer = None
            if peer is None or not allow_host(peer):
                self.close()
                raise EgressRefused(
                    "refused a connection that reached %r, which is neither "
                    "loopback nor an address a local docker bridge is carrying"
                    % (peer,))

    return PeerChecked


class _LocalOnlyHTTPHandler(urllib.request.HTTPHandler):
    """http:// through a peer-checked connection."""

    def __init__(self, allow_host):
        urllib.request.HTTPHandler.__init__(self)
        self._peer_checked = _peer_checked_connection(
            http.client.HTTPConnection, allow_host)

    def http_open(self, req):
        return self.do_open(self._peer_checked, req)


class _LocalOnlyHTTPSHandler(urllib.request.HTTPSHandler):
    """https:// through a peer-checked connection. Present because the feed URL
    knob accepts https, not because the bridge-only tracker is reached that way
    today."""

    def __init__(self, allow_host):
        urllib.request.HTTPSHandler.__init__(self)
        self._peer_checked = _peer_checked_connection(
            http.client.HTTPSConnection, allow_host)

    def https_open(self, req):
        return self.do_open(self._peer_checked, req, context=self._context)


def feed_opener(bridge_addresses=None):
    """The opener the FEED POST uses: no proxy, no redirect, no off-box peer.

    Three independent refusals, because validating the URL only says where we
    MEANT to go, and two mechanisms could still send the request elsewhere:

      * `ProxyHandler({})` installs NO proxy rather than reading the
        environment. An `http_proxy` exported into the unit would otherwise
        route the POST - bearer token, identity header and body weight -
        through a LAN proxy that then sees all of it, without the feed URL
        changing at all.
      * `_RefuseRedirects` refuses to be told where to go by the response.
      * the peer check asks the socket what address it actually reached and
        drops the connection before the request is written if that is not
        loopback or an address a local docker bridge is really carrying.

    Implements: SR-022, LLR-006
    """
    def allow_host(host):
        return is_local_destination(host, bridge_addresses)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirects(),
        _LocalOnlyHTTPHandler(allow_host),
        _LocalOnlyHTTPSHandler(allow_host))


def vendor_opener():
    """The opener THE BLOCKED VENDOR HALF MUST USE when it is written.

    It exists now, before the parser does, on purpose. The egress defect this
    round fixed happened because each call site reached for
    `urllib.request.urlopen` and inherited the default opener's behaviour, and
    the Google Health reader is the one call site in this repo that has not
    been written yet. Leaving it to invent its own door is how the same defect
    comes back; `tests/test_feeder_egress_parity.py` fails if any feeder grows
    a bare `urlopen` again.

    The decision here DIFFERS from the feed's, deliberately. A Google token
    refresh and a dataPoints list are OUTBOUND TO THE INTERNET by design, so
    the peer check would be nonsense and is absent. The other two stand:

      * REDIRECTS ARE REFUSED, because urllib carries `Authorization` across a
        cross-host redirect, and the token in question grants blood glucose,
        body fat, oxygen saturation, core temperature and heart-rate metrics
        as well as weight (there is no weight-only scope). A 302 from an
        impersonated endpoint would hand all of that to whatever host the
        `Location` names. The cost of refusing a legitimate redirect is an
        "unavailable" gauge, which is the outcome this feeder is built to
        produce whenever it cannot read the source honestly.
      * NO PROXY IS INHERITED FROM THE ENVIRONMENT, and there is deliberately
        no knob to opt back in: a proxy would terminate TLS in front of a
        health-data token. If an egress proxy is ever genuinely needed that is
        a requirement change, not a setting.

    Implements: SR-022, LLR-006
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseRedirects())


def post_gauge(body, url, env, timeout):
    """POST one body to /api/feed through the local-only opener -> (ok, detail).

    Never raises: a failed post is a reported failure, not a dead cycle.

    IT IS BODY-AGNOSTIC, and that is what makes the two posts INDEPENDENT.
    `run_cycle` calls it once with the gauge body and once with the check body;
    each call has its own (ok, detail), each failure is its own line, and
    neither can prevent or abort the other. The name is kept because it is the
    shared name this feeder and the usage feeder are held to by the egress
    parity test.

    THE REMOTE'S OWN WORDS ARE NEVER PUT IN `detail`. The previous version
    returned `exc.read()[:200]`, which `main` prints to stderr and systemd
    writes to the journal - so anything a responding server or an interposed
    proxy chose to reflect, INCLUDING a bearer token echoed back in an error
    body, was persisted to disk. The status code is ours to read; the body is
    the remote's to write, and it does not get a journal.

    Implements: SR-022, LLR-006
    """
    headers = {"Content-Type": "application/json"}
    token = env.get("WEIGHT_FEED_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    headers["X-Forwarded-User"] = env["_identity"]
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with feed_opener().open(request, timeout=timeout) as response:
            return response.status == 200, "HTTP %s" % response.status
    except EgressRefused as exc:
        return False, str(exc)           # our own words, not the remote's
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s (body not logged)" % exc.code
    except Exception as exc:
        return False, type(exc).__name__


def run_cycle(env, now=None, readers=None, poster=None, goal_loader=None,
              check_loader=None):
    """One feeder cycle: load the goal, read the source, post the gauge, and -
    only on a genuinely fresh weigh-in - post the automated check-off too.

    Contract:
      Inputs:  env: the process environment (plus `_identity` and `_feed_url`);
               `readers`, `poster`, `goal_loader` and `check_loader` are
               injectable so the tests can exercise every failure class without
               a network or a real definitions tree.
      Outputs: (posted, failures, goal_file). `posted` is a list of
               (id, fresh, ok): the gauge line ALWAYS first with `fresh` a
               bool, and - only on a cycle that actually ticked - a second line
               whose `fresh` is None, marking it as the check-off rather than a
               gauge.

    THE TWO POSTS ARE INDEPENDENT AND THE GAUGE GOES FIRST. Independent
    because each has its own try/except and its own failure line, so a 400 on
    the check cannot stop the wall from being told what the person weighs, and
    a dead tracker cannot stop the tick from being ATTEMPTED. Gauge first
    because it is the promise (SN-040: the panel must say what you weigh or say
    it does not know) and the tick is the extra: putting the extra first would
    put its latency and its failure modes in front of the thing that must
    always happen. `check_id_from_definitions` is not even consulted until the
    gauge has been posted, for the same reason - it reads the disk.

    THE TICK IS RECORDED ONLY IF IT LANDED. The sample time goes into the
    state file's `checked` block AFTER a 200, never before, so a tick that
    failed to post is retried on the next cycle instead of being remembered as
    done. Both the reading and the mark are written by the SAME `save_state`
    call, through the same `open_for_write` guard, into the same one file.

    THE ORDER IS DELIBERATE AND IT IS THE ONE THING TO GET RIGHT HERE. The goal
    is loaded FIRST - out of the `target` of the item the location knobs name -
    and a missing goal aborts the cycle WITHOUT posting. That
    is not the same refusal as a missing source:
      * no SOURCE  -> post an unavailable gauge, because the panel must say
        "we do not know what you weigh" rather than show nothing where a bar
        belongs;
      * no GOAL    -> post NOTHING, because the target line IS the goal.
        NagLight refuses a gauge without a target, and the only way to satisfy
        it would be to invent one - drawing a 50 lb bar around a number nobody
        chose and colouring a real body weight green or red against it.
    A cycle that cannot find a goal exits non-zero saying exactly which file to
    put it in, so the failure is legible in `systemctl status` rather than
    silent.

    Implements: SR-022, LLR-006
    """
    now = int(now if now is not None else time.time())
    readers = readers or SOURCE_READERS
    state_path = env.get("WEIGHT_STATE_FILE") or "/var/lib/homehub-weight/weight-state.json"
    state_root = resolve_state_root(env)
    defs_dir = env.get("WEIGHT_DEFINITIONS_DIR") or ""
    feed_url = env["_feed_url"]
    timeout = int(env.get("WEIGHT_TIMEOUT_SECONDS") or 30)
    source = enabled_source(env)
    # One clock for the whole cycle, so the reader judges a sample time against
    # the same `now` the gauge is judged by (see `cycle_now`).
    env["_now"] = now

    category, item_id = resolve_goal_location(env)
    goal_lb, goal_file = (goal_loader or load_goal_from_definitions)(
        defs_dir, category, item_id)

    reading, failures = None, []
    try:
        reading = readers[source](env)
    except SourceFailure as exc:
        failures.append("%s: %s" % (source, exc))
    except Exception as exc:
        # ONLY THE EXCEPTION TYPE, NEVER ITS MESSAGE. B7's pattern, copied here
        # because this is the branch the real OAuth reader will fall into: an
        # exception raised inside urllib carries the request object, and
        # `str(exc)` on one of those prints the Authorization header into the
        # journal. The type says what went wrong; the message says who we are.
        failures.append("%s: unexpected %s" % (source, type(exc).__name__))

    state = load_state(state_path, now)
    try:
        body, fresh = build_post(reading, state.get(GAUGE_ID), goal_lb, now)
    except (ValueError, SourceFailure) as exc:
        # A gauge this repo refuses to build must cost the READING, not the
        # post: the panel must still say "we do not know what you weigh"
        # rather than leave a hole where a bar belongs.
        failures.append("gauge %s: %s" % (GAUGE_ID, exc))
        body, fresh, reading = gauge_body(0.0, goal_lb, None), False, None
    try:
        ok, detail = (poster or post_gauge)(body, feed_url, env, timeout)
    except Exception as exc:               # a poster bug is a failed post
        ok, detail = False, type(exc).__name__
    if not ok:
        failures.append("post %s: %s" % (GAUGE_ID, detail))
    posted = [(GAUGE_ID, fresh, ok)]

    # ── THE SECOND POST. Everything below here is the check-off, and NOTHING
    # below here may change `body`, `ok` or the gauge's line above. ──────────
    marks = dict(state.get(CHECK_STATE_KEY) or {})
    check_id = None
    if reading is not None:
        # Only a cycle that read the source can possibly tick, so the
        # definitions are not re-read on the 95 cycles a week that cannot.
        check_id = (check_loader or check_id_from_definitions)(
            defs_dir, category, item_id)
    if check_id is not None and should_check_off(reading, marks.get(check_id), now):
        try:
            tick_body = check_body(check_id)
        except ValueError as exc:
            tick_body = None
            failures.append("check %s: %s" % (check_id, exc))
        if tick_body is not None:
            try:
                tick_ok, tick_detail = (poster or post_gauge)(
                    tick_body, feed_url, env, timeout)
            except Exception as exc:       # a poster bug is a failed post
                tick_ok, tick_detail = False, type(exc).__name__
            if not tick_ok:
                failures.append("post check %s: %s" % (check_id, tick_detail))
            else:
                # Remembered ONLY on a 200: an unremembered tick is retried,
                # a wrongly-remembered one is lost for the week.
                marks[check_id] = reading_pair(reading)[1]
            posted.append((check_id, None, tick_ok))

    if reading is not None:
        value_lb, observed_at = reading_pair(reading)
        state[GAUGE_ID] = {"value": value_lb, "observed_at": observed_at}
        if marks:
            state[CHECK_STATE_KEY] = marks
        try:
            save_state(state, state_path, state_root)
        except OSError as exc:
            # Including PermissionError from the write guard. The gauge is
            # already posted; losing the history is a named failure, not a
            # crash and never a fabricated reading.
            failures.append("state %s: %s" % (state_path, type(exc).__name__))
    return posted, failures, goal_file


def main(argv=None):
    """Entry point: gate, resolve, one cycle, report. It does not compute.

    Implements: SR-022
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    if not resolve_enabled(env):
        print("weight: WEIGHT_ENABLED is not true - nothing to do.")
        return 0
    env["_identity"] = resolve_identity(env)
    env["_feed_url"] = resolve_feed_url(env)
    enabled_source(env)
    if "--check" in argv:
        print("weight: configuration accepted (identity set, destination local).")
        return 0
    try:
        posted, failures, goal_file = run_cycle(env)
    except GoalMissing as exc:
        print("weight: REFUSED, nothing posted - %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("weight: REFUSED, nothing posted - %s" % exc, file=sys.stderr)
        return 2
    print("weight: goal read from %s" % goal_file)
    for key, fresh, ok in posted:
        # `fresh is None` marks the check-off line. It is a third state, not a
        # falsy one: "UNAVAILABLE" is a gauge word and would be a lie about a
        # tick that says a person stood on a scale.
        what = "checked off" if fresh is None else ("live" if fresh else "UNAVAILABLE")
        print("weight: %-10s %-12s %s" % (key, what,
                                          "posted" if ok else "POST FAILED"))
    for line in failures:
        print("weight: %s" % line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
