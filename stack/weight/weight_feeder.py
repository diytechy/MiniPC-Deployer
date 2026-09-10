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
THE VENDOR SOURCE IS VERIFIED BUT NOT YET REACHABLE. READ THIS BEFORE ADDING A
PARSER.
═══════════════════════════════════════════════════════════════════════════════

The build plan named "Google Health API v4". That API IS REAL, and every claim
below was established on 2026-09-09 by CALLING Google, not by reading a blog:

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

WHAT IS THEREFORE MISSING, AND ONLY THE OWNER CAN CLEAR IT:
  1. enable `health.googleapis.com` on the Google Cloud project that owns the
     household's one existing OAuth client - oauth2-proxy's, in
     OAUTH2_PROXY_CLIENT_ID / OAUTH2_PROXY_CLIENT_SECRET. There is no separate
     TRACKER_DRIVE_CLIENT_* pair: the live hub's .env was listed by key name on
     2026-09-09 and has none, and the tracker's Drive sync reads the
     OAUTH2_PROXY_* pair itself;
  2. add the scope above to that client's consent screen and add the Owner to
     the project's Test users list (projects start capped at 100 test users;
     going past that needs a third-party security review, which the household
     never will);
  3. sit at a browser, consent, and mint a refresh token into
     WEIGHT_TOKEN_FILE.

UNTIL A REAL RESPONSE BODY HAS BEEN SEEN, THIS FILE CONTAINS NO PARSER, ON
PURPOSE. B7 made that the standard and it is not negotiable here: a parser
written from a schema posts fiction the first time the schema is one field off,
and fiction shaped like a body weight is indistinguishable from a healthy
person. `read_google_health` REFUSES with a named blocker, which flows into the
ordinary unavailable path below, so the panel says "unavailable" - the truth -
instead of a number nobody measured. `test_weight_feeder.py` asserts that no
`parse_google_health` / `parse_weight_datapoint` symbol exists, exactly as B7
asserts there is no `parse_gemini`.

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

THE GOAL LIVES IN THE USER'S DEFINITIONS - see `load_goal_from_definitions`.

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
import urllib.request

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

# The one frontmatter key that carries the goal. See load_goal_from_definitions.
GOAL_KEY = "weight_goal_lb"

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


def grams_to_pounds(grams):
    """Convert the vendor's grams to the household's pounds.

    Google Health v4 stores body weight as `weightGrams` (a double) and NagLight
    infers its 50 lb bar from the unit string `lb`, so exactly one conversion
    exists on this path and it lives here rather than inline at the call site.
    The factor is the exact international-pound definition (0.45359237 kg), not
    a rounded 2.2046, because the panel shows one decimal and a rounded factor
    drifts visibly across the range.

    Implements: LLR-006
    """
    return check_pounds(grams, "grams") / 453.59237


def parse_goal_text(text, where):
    """Parse the goal value a definitions file declared, in pounds.

    Contract:
      Inputs:  text: the raw scalar exactly as written after `weight_goal_lb:`.
      Outputs: float in GOAL_MIN_LB..GOAL_MAX_LB.
      Raises:  GoalMissing for a blank; ValueError for anything unparseable or
               out of band, naming the file so the person can fix their own
               definitions.

    Quotes and a trailing `# comment` are stripped, because the definitions are
    hand-edited YAML frontmatter and both are ordinary things to write there.

    Implements: LLR-006
    """
    cleaned = (text or "").strip()
    if cleaned.startswith(("'", '"')) and len(cleaned) >= 2 and cleaned[-1] == cleaned[0]:
        cleaned = cleaned[1:-1].strip()
    else:
        cleaned = cleaned.split("#", 1)[0].strip()
    if not cleaned:
        raise GoalMissing("%s: %s is present but blank" % (where, GOAL_KEY))
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError("%s: %s is not a number: %r" % (where, GOAL_KEY, text))
    if not math.isfinite(value):
        raise ValueError("%s: %s is not finite: %r" % (where, GOAL_KEY, text))
    if value < GOAL_MIN_LB or value > GOAL_MAX_LB:
        raise ValueError(
            "%s: %s is %g lb, outside the sanity band %g..%g. This is a typo "
            "guard, not a judgement: a goal one digit out drags the inferred "
            "50 lb bar off the scale and paints every real reading full red."
            % (where, GOAL_KEY, value, GOAL_MIN_LB, GOAL_MAX_LB))
    return value


# `weight_goal_lb: 180` at the TOP LEVEL of the frontmatter - column zero, so an
# item field of the same name (which would be indented under a `- ` dash) can
# never be mistaken for the file-level goal.
GOAL_LINE_RE = re.compile(r"^%s\s*:\s*(.*)$" % re.escape(GOAL_KEY))


def find_goal_in_frontmatter(body, where):
    """Return the goal declared in ONE definitions file, or None.

    Contract:
      Inputs:  body: the whole .md file's text; where: its path, for messages.
      Outputs: float pounds, or None when this file declares no goal.
      Raises:  ValueError when the file declares a goal that is unusable, and
               GoalMissing when it declares the key with nothing after it. A bad
               declaration is NEVER treated as "no declaration": silently
               ignoring a typo'd goal is how a person ends up staring at a bar
               drawn around a number they thought they had changed.

    Only the leading YAML frontmatter (between the first two `---` fences) is
    read, matching internal/defs.frontmatter exactly. The prose body of a
    definitions file is free notes and a `weight_goal_lb:` mentioned there is
    someone writing about the goal, not declaring it.

    Implements: LLR-006
    """
    lines = body.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None                     # not a definitions file at all
    found = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            break                       # end of frontmatter
        if line.strip().startswith("items:"):
            break                       # the item sequence; nothing top-level after
        # DELIBERATELY REDUNDANT WITH THE `^` IN GOAL_LINE_RE AND WITH
        # `.match`, and the mutation run of 2026-09-09 proved it: removing this
        # check alone left the whole suite green (M25), because either of the
        # other two still refuses an indented line. It stays because it states
        # the intent where a reader looks for it, and because a future edit to
        # the pattern would otherwise silently promote an item field to the
        # household's goal. M27 removes all three at once and IS killed, so the
        # property is covered even though this one line is not individually.
        if line[:1] in (" ", "\t"):
            continue                    # indented: an item field, not file-level
        match = GOAL_LINE_RE.match(line)
        if match is None:
            continue
        if found is not None:
            raise ValueError(
                "%s declares %s twice; which one is authoritative is not "
                "guessable." % (where, GOAL_KEY))
        found = parse_goal_text(match.group(1), where)
    return found


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
        value_lb, observed_at = reading
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
    return open_no_follow(path)


def open_no_follow(path):
    """Open `path` for writing without ever following a symlink at the last hop.

    THE CHECK-THEN-OPEN RACE IS THE POINT. `writable_path_verdict` resolves the
    path, but a symlink planted between that resolution and a plain
    `open(path, "w")` would be followed by the open. So the file is unlinked
    first - which destroys a planted LINK and never the file it points at, and
    clears a `.tmp` left by a killed cycle - then created with O_CREAT|O_EXCL
    so the kernel refuses anything that appeared in the gap, and O_NOFOLLOW
    says the same again for the final component where the platform has it.

    O_NOFOLLOW DOES NOT EXIST ON WINDOWS and resolves to 0 there. The hub is
    Linux, so the deployed guard is whole; on the dev PC the unlink, O_EXCL and
    the resolving verdict still stand, which is what the tests exercise.
    """
    try:
        os.unlink(path)
    except OSError:
        pass          # absent is the normal case; a directory or a busy file
                      # fails loudly at the open below rather than here.
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")


# Second-line names only;
# Second-line names only; `open_for_write`'s allow-list is the real guard.
CREDENTIAL_BASENAMES = frozenset({
    "google-health-token.json",   # what WEIGHT_TOKEN_FILE normally is
    "credentials.json",
    ".credentials.json",
    "auth.json",
    ".netrc",
})


def load_state(state_path, now):
    """Last successful reading, VALIDATED. Anything else is {}.

    A missing or unparseable file is {} as before, and so now is an entry
    `validate_stored_reading` will not vouch for - because what this file holds
    is reposted to the wall as a body weight, and a state file is input rather
    than memory.
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
        checked = validate_stored_reading(entry, now)
        if checked is not None:
            out[key] = checked
    return out


def save_state(state, state_path, state_root):
    """Write the state file through `open_for_write`, atomically.

    The state holds A WEIGHT AND A TIMESTAMP ONLY - never a token, never a
    header, never a response body. That is asserted by test, because a state
    file that quietly grew a `refresh_token` would turn this feeder into the
    credential-writing thing it promises not to be.
    """
    tmp = state_path + ".tmp"
    with open_for_write(tmp, state_path, state_root) as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    # os.replace does NOT follow a symlink at the destination - it replaces the
    # link itself - so the rename cannot reach the token either, and the
    # verdict has already refused a state path resolving outside the root.
    os.replace(tmp, state_path)


def load_goal_from_definitions(defs_dir):
    """Read the goal out of the user's OWN definitions. Half the acceptance.

    Contract:
      Inputs:  defs_dir: the directory holding this user's definition files -
               the same `definitions/` NagLight loads with internal/defs.Load
               and keeps in step with Drive.
      Outputs: (goal_lb: float, source_file: str).
      Raises:  GoalMissing when the directory is absent, holds no *.md, or no
               file declares `weight_goal_lb`. ValueError when two files
               declare it, or when a declaration is unusable.

    WHY THE DEFINITIONS AND NOT A KNOB ON THE HUB. SN-040 says "the goal lives
    in the user's definitions so it syncs like everything else", and that is a
    behaviour statement, not a filing preference. Definitions are the one thing
    in this system a person edits from a phone and that arrives on the hub by
    itself. A goal in stack/.env would need an SSH session and a redeploy to
    change, would not travel with the rest of the person's tracker, and would
    be a second place the household's intent lives - which is how this repo's
    own /opt/homehub drift started.

    HOW IT SURVIVES NagLight WITHOUT A NagLight CHANGE. internal/defs/yaml.go
    parses top-level frontmatter scalars and its `default:` branch is
    `// ignore unknown top-level keys (forward-compatible)`. So a definitions
    file may carry

        ---
        category: health
        weight_goal_lb: 180
        items:
          - id: …
        ---

    and NagLight loads that file exactly as before. The goal is data the person
    keeps beside the things they track; the tracker does not need to understand
    it, because THIS feeder is what turns it into the gauge's target line.

    THE ONE PLACE IT DOES NOT YET SURVIVE, AND IT IS A REAL NagLight DEPENDENCY.
    Definitions reach the hub two ways. In FOLDER mode (drive.applyFolder) the
    .md bytes are staged verbatim and the key rides along untouched. In SHEET
    mode the sheet is exported to CSV and the .md files are REGENERATED from it
    by internal/defsheet, whose `columns` list is the item field set; an unknown
    column is collected into `unknown` and DROPPED. This household runs SHEET
    mode (TRACKER_DRIVE_SHEET_ID set, TRACKER_DRIVE_FOLDER_ID deliberately
    blanked on 2026-09-08), so on the deployed box the goal would be erased by
    the first sync after someone edited the sheet. Making it survive needs a
    change in internal/defsheet - a repo this block may not edit. Reported, not
    worked around: writing the goal to a hub knob "for now" would quietly make
    the acceptance criterion false while looking like it passed.

    Implements: SR-022, LLR-006
    """
    if not os.path.isdir(defs_dir):
        raise GoalMissing(
            "no definitions directory at %s. The goal lives in the user's own "
            "definitions (a top-level `%s:` in a definitions file's "
            "frontmatter), so with no definitions there is no goal and no "
            "honest bar to draw." % (defs_dir, GOAL_KEY))
    names = sorted(n for n in os.listdir(defs_dir) if n.endswith(".md"))
    found = None
    for name in names:
        path = os.path.join(defs_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
        except OSError as exc:
            raise ValueError("cannot read definitions file %s: %s"
                             % (path, type(exc).__name__))
        value = find_goal_in_frontmatter(body, path)
        if value is None:
            continue
        if found is not None:
            raise ValueError(
                "%s is declared in both %s and %s; which one is the goal is "
                "not guessable, and picking the first would silently follow "
                "file-name order." % (GOAL_KEY, found[1], path))
        found = (value, path)
    if found is None:
        raise GoalMissing(
            "no definitions file under %s declares a top-level `%s:`. Add it "
            "to the frontmatter of the health definitions file, e.g. "
            "`%s: 180`. It is NOT a hub setting on purpose: it belongs to the "
            "person, beside the things they track, so it syncs with them."
            % (defs_dir, GOAL_KEY, GOAL_KEY))
    return found


def read_google_health(env):
    """Source: Google Health API v4, Weight data type. NOT YET IMPLEMENTED.

    Contract:
      Outputs: (value_lb: float, observed_at: epoch seconds) - once the block
               above this module's docstring has been cleared.
      Raises:  SourceFailure, always, today.

    THIS FUNCTION DELIBERATELY CONTAINS NO PARSER, AND THAT IS THE POINT. The
    API, the data type, the route and the scope were all verified against
    Google on 2026-09-09 (see the module docstring for the calls and the
    responses), but no AUTHENTICATED call has ever been made, because minting
    a token needs the Owner at a browser consenting to a scope that has to be
    added to the OAuth client first. B7 made "one real call, verified, before
    the parser is written" this build's standard, and a weight parser written
    from a schema is the worst possible place to break it: if the field is one
    name off, or grams are actually kilograms, the feeder does not fail - it
    posts a confident, plausible, wrong body weight, and there is nothing on
    the panel that could tell anyone.

    So this refuses, by name, and the refusal flows into `build_post`'s
    ordinary unavailable path. The panel says "unavailable", which is the
    truth. When the Owner has minted a token, ONE list call is made by hand,
    its body is pasted into this module's docstring the way B7 pasted its
    three, and only then is `parse_weight_datapoint` written against it.

    Implements: LLR-006
    """
    token_file = (env.get("WEIGHT_TOKEN_FILE") or "").strip()
    have_token = bool(token_file) and os.path.exists(os.path.expanduser(token_file))
    raise SourceFailure(
        "google-health: BLOCKED on an Owner action, so no reading was taken "
        "and no number was invented. %s "
        "The Cloud project has health.googleapis.com enabled and the scope %s "
        "on its consent screen (Owner, 2026-09-09); what remains is to run "
        "`weight_oauth.py mint` and then `weight_oauth.py capture` on this box "
        "and write the parser against the body that comes back - the exact "
        "commands are in stack/weight/README.md. Until one real response body "
        "has been observed, this feeder posts 'unavailable' rather than a "
        "parser's guess."
        % ("A token file is present, but no parser exists yet - see the module "
           "docstring." if have_token else "No token file is present; run "
           "`weight_oauth.py mint`.",
           GOOGLE_HEALTH_SCOPE))


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
    """POST the gauge through the local-only opener. Returns (ok, detail).

    Never raises: a failed post is a reported failure, not a dead cycle.

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


def run_cycle(env, now=None, readers=None, poster=None, goal_loader=None):
    """One feeder cycle: load the goal, read the source, post one gauge.

    Contract:
      Inputs:  env: the process environment (plus `_identity` and `_feed_url`);
               `readers`, `poster` and `goal_loader` are injectable so the
               tests can exercise every failure class without a network or a
               real definitions tree.
      Outputs: (posted: list of (id, fresh, ok), failures: list of str).

    THE ORDER IS DELIBERATE AND IT IS THE ONE THING TO GET RIGHT HERE. The goal
    is loaded FIRST, and a missing goal aborts the cycle WITHOUT posting. That
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

    goal_lb, goal_file = (goal_loader or load_goal_from_definitions)(defs_dir)

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
    if reading is not None:
        state[GAUGE_ID] = {"value": reading[0], "observed_at": reading[1]}
        try:
            save_state(state, state_path, state_root)
        except OSError as exc:
            # Including PermissionError from the write guard. The gauge is
            # already posted; losing the history is a named failure, not a
            # crash and never a fabricated reading.
            failures.append("state %s: %s" % (state_path, type(exc).__name__))
    return [(GAUGE_ID, fresh, ok)], failures, goal_file


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
        print("weight: %-10s %-12s %s" % (key, "live" if fresh else "UNAVAILABLE",
                                          "posted" if ok else "POST FAILED"))
    for line in failures:
        print("weight: %s" % line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
