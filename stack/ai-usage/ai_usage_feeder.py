#!/usr/bin/env python3
"""The hub's AI-usage feeder: three vendor usage sources -> NagLight gauges.

ONE RESPONSIBILITY: read how much of each AI subscription has been consumed and
post that number to NagLight as a gauge. It decides nothing about colour and
holds no credential of its own.

WHY A PLAIN HUB SERVICE AND NOT A CONTAINER - the same reasoning A40 applied to
the AI CLI service (SR-019): two of the three sources are read through, or
beside, a credential that a human minted interactively into a home directory.
A container would have to mount that home, which punctures the isolation at the
only point that mattered, so containment comes from the account, the read-only
credential handling and the refused destinations instead.

THE THREE SOURCES, AND THE ONE REAL CALL EACH WAS BUILT FROM. Every parser in
this file was written against an observed response, not against documentation
(the build plan makes that a gate, because a speculative parser silently posts
fiction). The calls were made on 2026-09-09 from the dev PC:

  codex     `codex app-server --stdio`, JSON-RPC: `initialize` then
            `account/rateLimits/read` (params: null).  OBSERVED:
              {"id":2,"result":{"rateLimits":{"limitId":"codex",
                "primary":{"usedPercent":34,"windowDurationMins":10080,
                           "resetsAt":1789435411},
                "secondary":null,"credits":{...},"planType":"prolite",
                "rateLimitReachedType":null},
               "rateLimitsByLimitId":{"codex":{...},"codex_bengalfox":{...},
                                      "base_model_inference":{...}},
               "rateLimitResetCredits":{"availableCount":0,"credits":[]}}}
            NOTE `usedPercent` is an integer PERCENT (34 = 34%), and
            `windowDurationMins` 10080 = 7 days, so the codex gauge is a WEEKLY
            one. THIS FEEDER HANDLES NO CREDENTIAL FOR CODEX AT ALL: the child
            process reads its own `~/.codex/auth.json` and we only read stdout.

  claude    GET https://api.anthropic.com/api/oauth/usage
            Authorization: Bearer <the CLI's OAuth access token, read-only>
            anthropic-beta: oauth-2025-04-20
            User-Agent: claude-code/<version>
            OBSERVED 200 with:
              {"five_hour":{"utilization":79.0,"resets_at":"2026-09-09T09:10:00.468815+00:00",...},
               "seven_day":{"utilization":87.0,"resets_at":"2026-09-12T09:00:00.468835+00:00",...},
               "limits":[{"kind":"session","group":"session","percent":79,
                          "severity":"warning","resets_at":...,"is_active":false}, ...],
               "spend":{...}, ...}
            `utilization` is a FLOAT PERCENT. The payload also carries
            `severity` strings - WE DISCARD THEM. NagLight owns severity and
            colour; forwarding a vendor's opinion of "warning" would make the
            panel's authority ambiguous, which is the one thing the feed
            contract forbids.

  opencode  GET https://opencode.ai/zen/go/v1/usage
            Authorization: Bearer <workspace key, read-only>
            OBSERVED 200 with:
              {"usage":{"rolling":{"status":"ok","percent":0,"resetsAt":"...Z"},
                        "weekly":{"status":"ok","percent":58,"resetsAt":"...Z"},
                        "monthly":{"status":"ok","percent":89,"resetsAt":"...Z"}}}
            `percent` is an integer percent. `rolling` NAMES NO WINDOW LENGTH
            and the endpoint supplies none, so this feeder DOES NOT POST IT -
            see OPENCODE_BUCKETS. Inventing a length to satisfy the window
            field is exactly the fabricated reading the feed contract was
            tightened to stop.

Gemini is deliberately absent: deferred as E11.

THE FOUR ACCEPTANCE PROPERTIES, each with the symbol that enforces it:
  1. never writes a vendor credential file  -> `open_for_write`
  2. a failing source posts "unavailable", never a green gauge
                                            -> `build_post` (+ `is_fresh`)
  3. scoped to ONE explicit identity, refusing to guess
                                            -> `resolve_identity`
  4. off by default                         -> `resolve_enabled` / setup script

Implements: SR-021, LLR-005
"""

import http.client
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── The wire contract (NagLight IF-012 / this repo's IF-013) ─────────────────
# These are not tunables. They are the shape POST /api/feed accepts, and the
# 2026-09 tightening turned every one of them from "nice" into a 400:
#   * `value` and `target` must be PRESENT - an omitted value used to be stored
#     as a fabricated 0;
#   * `min`/`max` go together or are both omitted, and omission only infers a
#     range for units with an agreed width (`lb` alone today), so a percentage
#     feeder MUST send them;
#   * `direction` is required whenever `window` is present and REFUSED when it
#     is absent. Usage counts UP toward a cap, so it is always "up" here.
GAUGE_UNIT = "%"
GAUGE_MIN = 0.0
GAUGE_MAX = 100.0
GAUGE_TARGET = 0.0          # 0% consumed is the good end; we count up from it.
GAUGE_DIRECTION = "up"
RUNE_LIMIT = 120            # id/label/icon/unit are capped in RUNES, not bytes.

# NagLight's staleness horizons, by window kind. Kept here because the feeder's
# cadence has to be chosen against them: a WEEKLY gauge goes stale after 24h, so
# a weekly feeder that runs daily is permanently on the edge of "unavailable".
# AI_USAGE_INTERVAL defaults far below the tightest horizon for that reason.
STALE_HORIZON_SECONDS = {
    "daily": 36 * 3600,
    "weekly": 24 * 3600,
    "monthly": 48 * 3600,
    "static": 7 * 24 * 3600,
    None: 7 * 24 * 3600,
}

# THE HUB AND THE VENDORS DO NOT SHARE A CLOCK, so every comparison between a
# vendor's timestamp and ours gets this much slack and no more. It is small on
# purpose: its job is to absorb NTP jitter, not to make an expired window look
# current.
MAX_CLOCK_SKEW_SECONDS = 300

# No timestamp this feeder can legitimately see predates the block that wrote
# it (2025-01-01). A stored stamp below this is corruption, not history - see
# `validate_stored_reading`.
EPOCH_FLOOR = 1735689600

# The directory this service's own StateDirectory= gives it. The unit ships
# `StateDirectory=homehub-ai`, so systemd exports $STATE_DIRECTORY as exactly
# this; the literal is the fallback for a cycle run by hand, so a hand-run is
# bound the same way rather than left unbound. See `open_for_write`.
DEFAULT_STATE_ROOT = "/var/lib/homehub-ai"

# ── Vendor credential files: read, never written ────────────────────────────
# The acceptance criterion is "the feeder never writes a vendor credential
# file", and it is the property that keeps the household's INTERACTIVE
# subscriptions safe: these tokens were minted by a human in an RDP session and
# do not survive a reimage, so a feeder that truncated one would cost a person
# a login, and a feeder that rewrote one could park a token somewhere with
# looser permissions. The guard is not a naming convention - `open_for_write`
# refuses any path that is not the ONE state file this feeder owns.
CREDENTIAL_BASENAMES = frozenset({
    ".credentials.json",   # Claude Code OAuth
    "auth.json",           # codex and opencode
    "credentials.json",
    ".netrc",
})


class SourceFailure(Exception):
    """A source could not be read, or answered with something unusable.

    Raised for every failure class the acceptance criterion names - transport
    error, timeout, non-200, unparseable body, a well-formed body missing the
    field we need, and a percent that is not a finite number in 0..100. They
    are ONE exception on purpose: `build_post` must treat them identically, and
    a second exception type is a second chance to accidentally post a fresh
    reading for a source that failed.
    """


class EgressRefused(Exception):
    """A request tried to leave this box by a route the feeder does not permit.

    Raised by the redirect refusal and by the peer-address guard, and NOT a
    subclass of SourceFailure: the two callers convert it deliberately (a
    vendor redirect becomes an unavailable gauge; a feed redirect becomes a
    failed post), and nothing may catch it by accident on the way out.
    """


# ── Pure core: no I/O below this line until the SHELL banner ────────────────

def window_kind_for(duration_seconds):
    """Map a usage-window length to the NagLight `window.kind` vocabulary.

    Contract:
      Inputs:  duration_seconds: int > 0 - the real length of the vendor's
               counting window.
      Outputs: one of "daily" | "weekly" | "monthly" | "static".
      Raises:  SourceFailure if the duration is not a positive finite number.

    WHY BUCKETS AND NOT A PASS-THROUGH: `kind` is not decoration, it selects
    NagLight's staleness horizon, and an unrecognised kind silently gets the
    24h one. Claude's five-hour window has no kind of its own, so it takes the
    tightest horizon that cannot expire before the next post ("daily", 36h)
    rather than being sent as "five_hour" and landing in the unrecognised
    bucket by accident.

    Implements: LLR-005
    """
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise SourceFailure("window duration is not a number: %r" % (duration_seconds,))
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise SourceFailure("window duration is not positive and finite: %r" % (duration_seconds,))
    if duration_seconds <= 24 * 3600:
        return "daily"
    if duration_seconds <= 8 * 24 * 3600:
        return "weekly"
    if duration_seconds <= 32 * 24 * 3600:
        return "monthly"
    return "static"


def check_window(window_end, window_seconds, now, where):
    """Validate the vendor's counting window, or raise SourceFailure.

    Contract:
      Inputs:  window_end: the epoch second the window resets at, exactly as
                 the vendor sent it; window_seconds: its length in seconds;
                 now: this cycle's clock (see `cycle_now`); where: str for the
                 message.
      Outputs: (window_end: int, window_seconds: int).
      Raises:  SourceFailure when either end is absent or not a usable number,
               and when the window does not CONTAIN `now`.

    WHY A MISSING WINDOW IS A FAILURE RATHER THAN A GAUGE WITHOUT ONE.
    `window.kind` is not decoration - it SELECTS NagLight's staleness horizon,
    and a gauge with no window inherits the STATIC one, seven days. So a body
    that answered 200 but carried no `resets_at` used to become a gauge that
    stayed green for a week after the source died, which is the exact opposite
    of this feeder's one promise. A window we cannot determine now makes the
    reading unavailable instead, which is the honest answer.

    WHY THE WINDOW MUST CONTAIN `now`. A reset time already in the PAST is
    evidence that the body is a replay or a cached copy: the vendor is
    describing a window that has finished, so its percentage is not a statement
    about now, and stamping it `now` would be the fabrication `build_post`
    exists to prevent. A window that has not STARTED is the same evidence from
    the other side - a clock error, or milliseconds read as seconds, lands
    exactly there. MAX_CLOCK_SKEW_SECONDS is the only slack allowed.

    Implements: SR-021, LLR-005
    """
    if window_end is None or window_seconds is None:
        raise SourceFailure(
            "%s: the vendor named no usage window, so this reading cannot be "
            "posted. A windowless gauge inherits the 7-day static horizon and "
            "would stay green for a week after this source died." % where)
    window_kind_for(window_seconds)      # refuses 0, -1, NaN, inf, True, "week"
    if isinstance(window_end, bool) or not isinstance(window_end, (int, float)) \
            or not math.isfinite(window_end):
        raise SourceFailure("%s: window end is not a usable number: %r"
                            % (where, window_end))
    end = int(window_end)
    length = int(window_seconds)
    if end < now - MAX_CLOCK_SKEW_SECONDS:
        raise SourceFailure(
            "%s: the window reset at %d, which is already past - the body is a "
            "replay or a cached copy, not a reading about now" % (where, end))
    if end - length > now + MAX_CLOCK_SKEW_SECONDS:
        raise SourceFailure(
            "%s: the window has not begun (starts %d, now %d)"
            % (where, end - length, now))
    return end, length


def check_percent(raw, where):
    """Return `raw` as a float percent, or raise SourceFailure.

    Contract:
      Inputs:  raw: anything the vendor sent; where: str for the message.
      Outputs: float in [0.0, 100.0].
      Raises:  SourceFailure for None, a string, a bool, NaN, +/-inf, and
               anything outside 0..100.

    `bool` is rejected explicitly because `isinstance(True, int)` is True in
    Python and `True` would otherwise sail through as 1%.

    Implements: LLR-005
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SourceFailure("%s: percent is not a number: %r" % (where, raw))
    value = float(raw)
    if not math.isfinite(value):
        raise SourceFailure("%s: percent is not finite: %r" % (where, raw))
    if value < 0.0 or value > 100.0:
        raise SourceFailure("%s: percent outside 0..100: %r" % (where, raw))
    return value


def parse_iso8601_utc(text, where):
    """Parse an RFC3339/ISO-8601 timestamp to epoch seconds (int).

    Both HTTP sources stamp their resets this way; codex uses epoch integers
    instead and does not come through here. A `Z` suffix is normalised because
    `datetime.fromisoformat` on the Python versions this box ships does not
    accept it.

    Raises: SourceFailure on anything unparseable.

    Implements: LLR-005
    """
    if not isinstance(text, str) or not text:
        raise SourceFailure("%s: timestamp is not a string: %r" % (where, text))
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise SourceFailure("%s: unparseable timestamp %r (%s)" % (where, text, exc))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


class Reading(object):
    """One vendor number, with the window it was counted over.

    A Reading is what a source RETURNS; a gauge is what we POST. Keeping them
    apart is what lets `build_post` treat "no reading" as one case rather than
    scattering the unavailable path through three parsers.
    """

    __slots__ = ("percent", "window_end", "window_seconds")

    def __init__(self, percent, window_end, window_seconds):
        self.percent = percent
        self.window_end = window_end            # epoch seconds, or None
        self.window_seconds = window_seconds    # window length, or None

    def __repr__(self):
        return "Reading(percent=%r, window_end=%r, window_seconds=%r)" % (
            self.percent, self.window_end, self.window_seconds)


class GaugeSpec(object):
    """The unchanging half of one gauge: its upsert id, label and icon.

    `key` is BOTH the NagLight upsert id and the key in this feeder's state
    file, deliberately: a repost replaces by id, and the last-known reading we
    fall back to on a failure must be the reading for that same id.
    """

    __slots__ = ("key", "label", "icon", "source")

    def __init__(self, key, label, icon, source):
        self.key = key
        self.label = label
        self.icon = icon
        self.source = source


def parse_codex(payload, now):
    """Turn one `account/rateLimits/read` result into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the JSON-RPC `result` object, exactly as observed;
               now: this cycle's clock, against which the window is checked.
      Outputs: dict of gauge key -> Reading. Empty is NOT valid; a payload with
               no usable bucket raises rather than reporting "all clear".
      Raises:  SourceFailure on a non-object, a missing or null `rateLimits`, a
               missing `primary`, an unusable `usedPercent`, or a window that
               `check_window` refuses.

    THE WINDOW IS VALIDATED HERE, INSIDE THE READER'S CATCH, AND THAT PLACEMENT
    IS THE FIX FOR A REAL DEFECT. `usedPercent: 34` with a `windowDurationMins`
    of 0, -1, NaN or infinity used to parse cleanly and only blow up later in
    `build_post`, OUTSIDE the per-source try - so one vendor's malformed window
    aborted the whole cycle before ANY gauge was posted, and every other
    source's previously-fresh value sat green on the panel until it expired.

    Only the BACKWARD-COMPATIBLE single-bucket `rateLimits` view is read.
    `rateLimitsByLimitId` was observed carrying three buckets whose membership
    is a vendor implementation detail (`codex`, `codex_bengalfox`,
    `base_model_inference`); gauges keyed off it would appear and vanish from
    the panel as the vendor renames a model, so the panel gets the one bucket
    the vendor itself calls the compatible view.

    Implements: LLR-005
    """
    if not isinstance(payload, dict):
        raise SourceFailure("codex: result is not an object")
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict):
        raise SourceFailure("codex: no rateLimits object in result")
    primary = limits.get("primary")
    if not isinstance(primary, dict):
        raise SourceFailure("codex: rateLimits.primary is absent or null")
    percent = check_percent(primary.get("usedPercent"), "codex primary")

    minutes = primary.get("windowDurationMins")
    # Junk survives the multiply as junk (NaN*60 is NaN, True is left alone)
    # and check_window refuses it on type or on value, so exactly one place in
    # this file decides what a usable window is.
    seconds = (minutes * 60
               if isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
               else minutes)
    window_end, window_seconds = check_window(
        primary.get("resetsAt"), seconds, now, "codex primary")
    return {"ai-usage-codex": Reading(percent, window_end, window_seconds)}


# Claude's payload names its windows in the FIELD NAME, and the observed body
# carries no duration field, so the length is read off the name. Each entry is
# (payload key, gauge key, label, window length in seconds).
CLAUDE_BUCKETS = (
    ("five_hour", "ai-usage-claude-session", "Claude session", 5 * 3600),
    ("seven_day", "ai-usage-claude-weekly", "Claude weekly", 7 * 24 * 3600),
)


def parse_claude(payload, now):
    """Turn one OAuth usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed;
               now: this cycle's clock, against which each window is checked.
      Outputs: dict of gauge key -> Reading, one per CLAUDE_BUCKETS entry that
               is present and usable.
      Raises:  SourceFailure on a non-object or when NO bucket is usable. A
               body whose `five_hour` is null but whose `seven_day` is fine
               yields one gauge and no exception - the two are independent
               subscriptions limits and one being absent is not a failure of
               the other. A bucket whose `resets_at` is MISSING, or already in
               the past, is skipped for the same reason a null one is: the
               window is what selects NagLight's staleness horizon, so a
               windowless bucket would post a gauge that stayed green for the
               static seven days instead of the 24-48h its window implies.

    `severity` and `limits[].severity` are read and DISCARDED. The vendor's
    opinion of red is not NagLight's, and only one of them may own the panel's
    colour.

    Implements: LLR-005
    """
    if not isinstance(payload, dict):
        raise SourceFailure("claude: body is not an object")
    out = {}
    problems = []
    for field, key, _label, window_seconds in CLAUDE_BUCKETS:
        bucket = payload.get(field)
        if not isinstance(bucket, dict):
            problems.append("%s absent or null" % field)
            continue
        try:
            percent = check_percent(bucket.get("utilization"), "claude " + field)
            resets_at = bucket.get("resets_at")
            window_end, window_seconds = check_window(
                None if resets_at is None
                else parse_iso8601_utc(resets_at, "claude " + field),
                window_seconds, now, "claude " + field)
        except SourceFailure as exc:
            problems.append(str(exc))
            continue
        out[key] = Reading(percent, window_end, window_seconds)
    if not out:
        raise SourceFailure("claude: no usable bucket (%s)" % "; ".join(problems))
    return out


# OpenCode's bucket names ARE the window lengths - except `rolling`, which
# names none and for which the endpoint supplies none. It is therefore absent
# from this table and is never posted: a window we would have had to invent is
# a fabricated reading, and `direction` is refused without a window anyway, so
# the alternative was a gauge with a 7-day staleness horizon that stays green
# for a week after the feeder dies. Recorded as a known gap in README.md.
OPENCODE_BUCKETS = (
    ("weekly", "ai-usage-opencode-weekly", "OpenCode weekly", 7 * 24 * 3600),
    ("monthly", "ai-usage-opencode-monthly", "OpenCode monthly", 30 * 24 * 3600),
)


def parse_opencode(payload, now):
    """Turn one Zen usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed
               ({"usage":{"weekly":{"status","percent","resetsAt"}, ...}});
               now: this cycle's clock, against which each window is checked.
      Outputs: dict of gauge key -> Reading.
      Raises:  SourceFailure on a non-object, a missing `usage`, or when no
               listed bucket is usable.

    A bucket whose `status` is not "ok" is skipped rather than posted: the
    vendor is telling us the number is not trustworthy, and a number we were
    warned about is exactly the one that must not become a green gauge. A
    bucket missing `resetsAt`, or carrying one that has already passed, is
    skipped by `check_window` for the same reason `rolling` is not in the table
    above: a gauge with no window takes the 7-day static horizon and stays
    green long after the source has stopped answering.

    Implements: LLR-005
    """
    if not isinstance(payload, dict):
        raise SourceFailure("opencode: body is not an object")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise SourceFailure("opencode: no usage object in body")
    out = {}
    problems = []
    for field, key, _label, window_seconds in OPENCODE_BUCKETS:
        bucket = usage.get(field)
        if not isinstance(bucket, dict):
            problems.append("%s absent" % field)
            continue
        status = bucket.get("status")
        if status != "ok":
            problems.append("%s status=%r" % (field, status))
            continue
        try:
            percent = check_percent(bucket.get("percent"), "opencode " + field)
            resets_at = bucket.get("resetsAt")
            window_end, window_seconds = check_window(
                None if resets_at is None
                else parse_iso8601_utc(resets_at, "opencode " + field),
                window_seconds, now, "opencode " + field)
        except SourceFailure as exc:
            problems.append(str(exc))
            continue
        out[key] = Reading(percent, window_end, window_seconds)
    if not out:
        raise SourceFailure("opencode: no usable bucket (%s)" % "; ".join(problems))
    return out


GAUGE_SPECS = (
    GaugeSpec("ai-usage-codex", "Codex weekly", "🤖", "codex"),
    GaugeSpec("ai-usage-claude-session", "Claude session", "🧠", "claude"),
    GaugeSpec("ai-usage-claude-weekly", "Claude weekly", "🧠", "claude"),
    GaugeSpec("ai-usage-opencode-weekly", "OpenCode weekly", "🧩", "opencode"),
    GaugeSpec("ai-usage-opencode-monthly", "OpenCode monthly", "🧩", "opencode"),
)


def build_gauge(spec, value, observed_at, window_end, window_seconds):
    """Assemble one POST body in the exact shape /api/feed accepts.

    Contract:
      Inputs:  spec: GaugeSpec; value: float percent in 0..100;
               observed_at: epoch seconds when the value was TRUE, or None;
               window_end/window_seconds: the vendor's window, or None/None.
      Outputs: dict ready to json-encode.
      Raises:  ValueError if this repo would emit a body the server must 400 -
               a non-finite value, a rune-oversize id/label/icon/unit, or a
               window without both ends.

    THREE RULES ARE ENFORCED HERE RATHER THAN TRUSTED:
      * `value` and `target` are always present. An omitted value is a 400 now,
        and used to be stored as a fabricated 0 - the very reading this feeder
        must never invent.
      * `min`/`max` are always sent. Omission only infers a range for units
        with an agreed width, and `%` is not one of them.
      * `direction` is emitted IF AND ONLY IF `window` is - required with,
        refused without.
      * A BODY THAT CARRIES `observed_at` MUST CARRY A WINDOW. `window.kind`
        selects NagLight's staleness horizon, so a stamped gauge with no window
        silently takes the STATIC one - seven days - instead of the 24-48h the
        vendor's real window implies, and a source that died stays green for a
        week. The one body that legitimately has no window is the
        never-measured sentinel, and it is exactly the body with no stamp.
    NO COLOUR, NO SEVERITY, NO `css` FIELD IS EVER SET. NagLight derives them;
    a feeder that computes a hue makes the panel's authority ambiguous.

    Implements: SR-021, LLR-005
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError("gauge %s: value is not a finite number: %r" % (spec.key, value))
    for name, text in (("id", spec.key), ("label", spec.label),
                       ("icon", spec.icon), ("unit", GAUGE_UNIT)):
        if len(text) > RUNE_LIMIT:
            raise ValueError("gauge %s: %s exceeds %d runes" % (spec.key, name, RUNE_LIMIT))
    if not math.isfinite(GAUGE_MAX - GAUGE_MIN):
        raise ValueError("gauge %s: non-finite min..max span" % spec.key)
    if observed_at is not None and (window_end is None or window_seconds is None):
        raise ValueError(
            "gauge %s: a body carrying observed_at must carry the window it was "
            "counted over - a windowless gauge inherits the 7-day static "
            "horizon and would stay green long after this source died"
            % spec.key)

    body = {
        "kind": "gauge",
        "id": spec.key,
        "label": spec.label,
        "icon": spec.icon,
        "unit": GAUGE_UNIT,
        "value": float(value),
        "min": GAUGE_MIN,
        "max": GAUGE_MAX,
        "target": GAUGE_TARGET,
    }
    if window_end is not None and window_seconds is not None:
        body["window"] = {
            "start": int(window_end) - int(window_seconds),
            "end": int(window_end),
            "kind": window_kind_for(window_seconds),
        }
        body["direction"] = GAUGE_DIRECTION
    if observed_at is not None:
        body["observed_at"] = int(observed_at)
    return body


def validate_stored_reading(entry, now):
    """Return a stored reading only if it is credible history, else None.

    Contract:
      Inputs:  entry: whatever `load_state` found under this gauge's key -
               any JSON value at all, including None; now: this cycle's clock.
      Outputs: a dict with exactly value/observed_at/window_end/window_seconds,
               all numbers, or None if the entry cannot be used as history.
      Raises:  nothing. An unusable entry is an ANSWER, not an exception - the
               caller's job is then identical to a source that never succeeded.

    THE STATE FILE IS INPUT, NOT MEMORY. It is a file on disk that a
    half-written cycle, a disk error, or a person with an editor can change,
    and everything downstream treats what it holds as a reading that was once
    TRUE: it is reposted at its own stamp and the panel renders it. Nothing
    used to check it - `build_gauge` only asked whether the value was finite -
    so a corrupted entry could post `100000` percent, or carry a stamp in the
    FUTURE and make a source that has been dead for days look live.

    A nonsensical stored reading is not history, it is a failure, and it gets
    the same honest answer a failed source with no history gets: value 0, no
    stamp, "unavailable".

    WHAT IS CHECKED, AND WHY EACH: the percent is re-run through the same
    `check_percent` a vendor number faces, because state is no more trusted
    than a vendor; the stamp must lie between EPOCH_FLOOR and now (a FUTURE
    stamp is the one that fabricates freshness, so it is refused rather than
    clamped); the window must be one `window_kind_for` recognises, since it is
    what NagLight's horizon is chosen from; and the stamp must fall INSIDE the
    window it claims, because a reading taken outside the window it names is
    two facts that cannot both be true.

    Implements: SR-021, LLR-005
    """
    if not isinstance(entry, dict):
        return None
    try:
        value = check_percent(entry.get("value"), "stored value")
    except SourceFailure:
        return None
    numbers = {}
    for name in ("observed_at", "window_end", "window_seconds"):
        raw = entry.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                or not math.isfinite(raw):
            return None
        numbers[name] = int(raw)
    stamp = numbers["observed_at"]
    end = numbers["window_end"]
    length = numbers["window_seconds"]
    if stamp < EPOCH_FLOOR or stamp > now + MAX_CLOCK_SKEW_SECONDS:
        return None
    if end < EPOCH_FLOOR:
        return None
    try:
        window_kind_for(length)
    except SourceFailure:
        return None
    if not (end - length - MAX_CLOCK_SKEW_SECONDS
            <= stamp <= end + MAX_CLOCK_SKEW_SECONDS):
        return None
    return {"value": value, "observed_at": stamp,
            "window_end": end, "window_seconds": length}


def build_post(spec, reading, last, now):
    """Decide what to POST for one gauge this cycle - the "never green" rule.

    Contract:
      Inputs:  spec: GaugeSpec;
               reading: Reading if THIS cycle read the source successfully,
                        else None (every SourceFailure class collapses to None);
               last:    the stored {"value","observed_at","window_end",
                        "window_seconds"} from a previous successful cycle, or
                        None if this gauge has never had one. It is
                        RE-VALIDATED here even though `load_state` already
                        validated it, because the guard has to hold at the
                        point of use as well as at the point of load - the two
                        are separated by every source read in the cycle;
               now:     epoch seconds.
      Outputs: (body: dict, fresh: bool). `fresh` is True only when the body
               carries THIS cycle's timestamp.
      Raises:  ValueError only for a body this repo should never build.

    THE INVARIANT, and it is the acceptance criterion in one line:
        observed_at == now  IF AND ONLY IF  reading is not None.
    Everything else follows from NagLight's own rule that `observed_at` is when
    the value was TRUE, so:
      * source read      -> the real number, stamped now -> a live gauge;
      * source failed, a previous reading exists -> that number, stamped WHEN
        IT WAS TRUE. It is not a lie and it goes stale on the horizon for its
        window kind, so the panel shows "unavailable" instead of a stale green;
      * source failed and there is no previous reading -> value 0 with NO
        `observed_at` at all, which NagLight treats as stale on arrival. The
        gauge EXISTS so the panel can say "unavailable" rather than showing
        nothing, and the 0 is never rendered as a live reading. This is the one
        number in the file we did not measure, and it is unreachable as a
        displayed value by construction.

    Implements: SR-021, LLR-005
    """
    if reading is not None:
        return build_gauge(spec, reading.percent, now,
                           reading.window_end, reading.window_seconds), True
    checked = validate_stored_reading(last, now)
    if checked is not None:
        return build_gauge(spec, checked["value"], checked["observed_at"],
                           checked["window_end"], checked["window_seconds"]), False
    return build_gauge(spec, 0.0, None, None, None), False


def is_fresh(body, now):
    """True if NagLight would still consider this body's reading current.

    Used by the tests to assert the "never a green gauge" property directly
    against NagLight's horizon table rather than against our own intent: a
    body with no `observed_at`, or one older than the horizon for its window
    kind, or one stamped in the future, is not fresh.

    Implements: LLR-005
    """
    stamp = body.get("observed_at")
    if stamp is None:
        return False
    if stamp > now:
        return False        # a future stamp is stale by NagLight's rule.
    kind = body.get("window", {}).get("kind")
    return (now - stamp) <= STALE_HORIZON_SECONDS.get(kind, 24 * 3600)


def cycle_now(env):
    """The single instant this cycle reasons about, in epoch seconds.

    `run_cycle` puts it in the environment alongside `_identity` and
    `_feed_url` so that the readers - and through them the parsers - measure a
    vendor's window against the SAME `now` the gauge will be stamped with. A
    parser that called `time.time()` for itself could accept a window that
    expired between the read and the post, which is precisely the "fresh stamp
    on a stale reading" this round was sent back to fix.

    Implements: LLR-005
    """
    raw = env.get("_now")
    return int(raw) if raw is not None else int(time.time())


def resolve_identity(env):
    """Return the ONE household identity these gauges are posted for.

    Contract:
      Config:  AI_USAGE_USER - the stable Google `sub`, exactly as
               TRACKER_DRIVE_USER / TRACKER_MIRROR_USER / NAGLIGHT_USER are.
      Raises:  SystemExit(2) when it is unset or blank.

    IT REFUSES TO GUESS, and erroring out is the refusal - not a default. The
    tracker's own precedent is the argument: syncing or mirroring the WRONG
    household member overwrites someone else's data, and posting a gauge to the
    wrong person's board is the same fault with a friendlier face. There is no
    "the only user" fallback because a single-user box today is a two-user box
    after one `oauth2-proxy` login.

    Implements: SR-021, LLR-005
    """
    value = (env.get("AI_USAGE_USER") or "").strip()
    if not value:
        raise SystemExit(
            "REFUSED: AI_USAGE_USER is unset. The feeder posts for ONE explicit "
            "identity (the stable Google `sub`, the same value as NAGLIGHT_USER) "
            "and will not guess which household member's board these gauges "
            "belong on. Set it in stack/.env, or set AI_USAGE_ENABLED=false.")
    return value


def resolve_enabled(env):
    """True only for a literal `true`. Off is the default and every typo.

    Gated exactly like REMOTE_UI_ENABLED and AI_CLI_ENABLED: an unset knob, a
    blank one, `True`, `yes` and `1` are all OFF, because a feeder that starts
    on an ambiguous value starts on a box nobody asked.

    Implements: SR-021, LLR-005
    """
    return (env.get("AI_USAGE_ENABLED") or "false").strip() == "true"


def resolve_state_root(env):
    """The ONE directory under which this feeder may write, whatever the config says.

    Contract:
      Config:  STATE_DIRECTORY - exported by systemd for a unit carrying
               `StateDirectory=`, which this one does (`StateDirectory=homehub-ai`).
      Outputs: an absolute path; DEFAULT_STATE_ROOT when the variable is absent.

    WHY THIS EXISTS SEPARATELY FROM AI_USAGE_STATE_FILE. The allow-list in
    `open_for_write` is built from a path the CONFIG names, so on its own it
    proves only that the feeder writes where it was told - it cannot tell a
    state file from a vendor token if the knob names a token. Binding the write
    to the service's OWN state directory makes the guard independent of the
    .env: nothing outside /var/lib/homehub-ai is writable however that file is
    edited, and the unit's StateDirectoryMode=0700 backs it from the other side.

    Only one directory is ever named here (systemd would colon-separate a list,
    and the unit declares exactly one), so the value is taken whole rather than
    split - splitting would mangle a Windows dev path in the tests.

    Implements: SR-021, LLR-005
    """
    return (env.get("STATE_DIRECTORY") or "").strip() or DEFAULT_STATE_ROOT


def resolve_feed_url(env):
    """Return the /api/feed URL, refusing a destination off this box.

    Contract:
      Config:  AI_USAGE_FEED_URL - required, no default.
      Raises:  SystemExit(2) when unset, when the scheme is not http/https, or
               when the host is not loopback and not an address a local docker
               bridge is carrying.

    WHY THE HOST IS CHECKED AND NOT JUST THE SCHEME. The POST carries the
    tracker bearer token and the household identity in `X-Forwarded-User`. The
    tracker is bridge-only by design (no host publish), so the only legitimate
    destinations are its own loopback and its bridge address; the public https
    route would bounce through oauth2-proxy and OVERWRITE the identity header,
    and any other host would hand a household token to a stranger. This mirrors
    ai_cli_service.resolve_bind, which learned the hard way that "the docker
    bridge" must mean an address an interface actually carries and not the
    whole of 172.16.0.0/12 - a range containing real household LANs.

    Implements: SR-021, LLR-005
    """
    raw = (env.get("AI_USAGE_FEED_URL") or "").strip()
    if not raw:
        raise SystemExit(
            "REFUSED: AI_USAGE_FEED_URL is unset. There is no default: the "
            "tracker is bridge-only, so the right value depends on this box.")
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(raw)
    except Exception as exc:                      # pragma: no cover - defensive
        # The TYPE only, never the message: urlsplit echoes the value it was
        # given, and a feed URL is allowed to carry userinfo, so the message
        # could put `http://user:token@host/` into the journal.
        raise SystemExit("REFUSED: AI_USAGE_FEED_URL is unparseable (%s)"
                         % type(exc).__name__)
    if parts.scheme not in ("http", "https"):
        raise SystemExit("REFUSED: AI_USAGE_FEED_URL scheme %r is not http/https" % parts.scheme)
    host = parts.hostname or ""
    if not host:
        raise SystemExit("REFUSED: AI_USAGE_FEED_URL has no host")
    if not is_local_destination(host):
        raise SystemExit(
            "REFUSED: AI_USAGE_FEED_URL host %r is neither loopback nor an "
            "address a local docker bridge is carrying. The POST carries the "
            "tracker token and the household identity; it must not leave this "
            "box." % host)
    return raw


def is_local_destination(host, bridge_addresses=None):
    """True for loopback or an address a LOCAL docker bridge really carries.

    `bridge_addresses` is injectable so the tests can state the box's bridges
    instead of depending on whatever this machine happens to have. A hostname
    is refused outright rather than resolved: resolution is a moving target and
    `localhost.example.com` is a real trick.

    Implements: LLR-005
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


# ── SHELL: everything below touches the world ───────────────────────────────

def local_bridge_networks():
    """The networks the box's docker bridges are actually carrying.

    Reads `ip -json addr` and keeps only `docker0` and `br-*`. An empty list is
    a legitimate answer (a box with no bridge), and it means only loopback is
    an acceptable destination - which is the safe direction to fail.
    """
    import ipaddress
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
               state_path: the one file this feeder owns (AI_USAGE_STATE_FILE);
               state_root: the directory this service's StateDirectory= gave it;
               resolve: the path resolver - `os.path.realpath` in production,
                 injected only so a test can state what a symlink resolves to
                 on a filesystem that will not let it create one.
      Outputs: None if the write is allowed, else the reason it is refused.
      Raises:  nothing. It DECIDES; `open_for_write` is what refuses.

    WHY realpath AND NOT abspath - THE CROSS-REVIEW DEFECT THIS FUNCTION EXISTS
    TO CLOSE. `os.path.abspath` is string arithmetic: it normalises `..` and
    makes the path absolute, and it does NOT resolve symlinks. So pre-creating
    `<state>.tmp` as a symlink pointing at a vendor token passed the old
    allow-list unchanged - the STRING matched - and `save_state` then opened it
    "w" and truncated the household's token. The shape of the allow-list was
    right all along; the resolution was wrong.

    THE STATE ROOT IS A SECOND, INDEPENDENT BOUND, and it is independent in the
    way that matters: the allow-list is derived from a path the CONFIG names,
    so it can only ever say "the feeder wrote where it was told". Requiring the
    resolved path to sit inside the service's own StateDirectory means a .env
    that names a token is refused too, because /home/... is not
    /var/lib/homehub-ai whatever the knob says.

    Implements: SR-021, LLR-005
    """
    resolve = resolve or os.path.realpath
    # The basename is read off BOTH the name we were handed and the file it
    # really resolves to: the first names the hazard when someone points the
    # state knob at a token, the second when a link does the pointing.
    for named in (path, resolve(path)):
        if os.path.basename(named) in CREDENTIAL_BASENAMES:
            return ("%s (which resolves to %s) is a vendor credential file. "
                    "This feeder reads credentials and never writes one."
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
    """The ONLY way this feeder opens a file for writing. THE credential guard.

    Contract:
      Inputs:  path/state_path/state_root as `writable_path_verdict` takes them.
      Outputs: an open text-mode handle.
      Raises:  PermissionError for anything the verdict refuses, and OSError
               from the kernel if the final component is a symlink or if the
               temp file appeared between the verdict and the open.

    WHY AN ALLOW-LIST AND NOT A DENY-LIST. "Never writes a vendor credential
    file" cannot be met by listing the files we know about - the next CLI
    version puts its token somewhere new. So the feeder declares the one path
    it may write and refuses the rest of the filesystem, and the credential
    basenames are a SECOND check that exists only to make the failure message
    name the real hazard when someone points the state file at a token.

    THE VERDICT IS NOT TRUSTED AT THE OPEN - see `open_no_follow`. Checking a
    path and then opening it is two operations with a gap between them, and the
    attack this guard was rewritten for is precisely a symlink planted into
    that gap.

    Implements: SR-021, LLR-005
    """
    reason = writable_path_verdict(path, state_path, state_root)
    if reason:
        raise PermissionError("REFUSED: " + reason)
    return open_no_follow(path)


def open_no_follow(path):
    """Open `path` for writing without ever following a symlink at the last hop.

    THE CHECK-THEN-OPEN RACE IS THE POINT, and it is why the verdict alone was
    not accepted as the fix. `writable_path_verdict` resolves the path, but a
    symlink planted between that resolution and a plain `open(path, "w")` would
    be followed by the open. So:

      * the file is unlinked first. Unlinking removes THE SYMLINK ITSELF and
        never the file it points at, so a planted link is destroyed rather than
        traversed - and a `.tmp` left behind by a killed cycle is cleared in
        the same stroke, which is why O_EXCL below does not wedge the feeder;
      * O_CREAT|O_EXCL then makes the kernel refuse if ANYTHING is at that path
        by the time we open, symlink included - so the window between the
        unlink and the open is closed by the kernel and not by our confidence;
      * O_NOFOLLOW says the same thing again for the final component, on the
        platform that has it.

    O_NOFOLLOW DOES NOT EXIST ON WINDOWS and resolves to 0 there. The hub is
    Linux, so the deployed guard is whole; on the Windows dev PC the unlink and
    O_EXCL still stand and the verdict still resolves symlinks, which is what
    the tests exercise.
    """
    try:
        os.unlink(path)
    except OSError:
        pass          # absent is the normal case; a directory or a busy file
                      # will fail loudly at the open below rather than here.
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")


def load_state(state_path, now):
    """Last successful reading per gauge key, VALIDATED. Anything else is {}.

    A missing or unparseable file is {} as before, and so now is any individual
    entry `validate_stored_reading` will not vouch for. Dropping a bad entry at
    the door rather than at the point of use means the rest of the file still
    works: one corrupted gauge goes "unavailable" and its four neighbours keep
    their real history.
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

    The state holds PERCENTAGES AND TIMESTAMPS ONLY - never a token, never a
    header, never a response body. That is asserted by test, because a state
    file that quietly grew an `Authorization` value would turn this feeder into
    the credential-writing thing it promises not to be.
    """
    tmp = state_path + ".tmp"
    with open_for_write(tmp, state_path, state_root) as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    # os.replace does NOT follow a symlink at the destination - it replaces the
    # link itself - so the rename cannot reach a token either, and the verdict
    # has already refused a state path that resolves outside the state root.
    os.replace(tmp, state_path)


def read_secret_file(path, extractor):
    """Read a vendor credential READ-ONLY and hand back one string.

    Opened in "r" mode through the builtin and never through `open_for_write`,
    so there is no code path in this file that can write one. The value is
    returned to exactly one caller, put in exactly one Authorization header,
    and never logged, never stored and never echoed on failure.
    """
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SourceFailure("credential %s unreadable: %s" % (path, type(exc).__name__))
    value = extractor(data)
    if not value:
        raise SourceFailure("credential %s carries no usable key" % path)
    return value


def _claude_token(data):
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if isinstance(value, str) and key.lower() in ("accesstoken", "access_token"):
            return value
        found = _claude_token(value)
        if found:
            return found
    return None


def _opencode_key(data):
    if not isinstance(data, dict):
        return None
    entry = data.get("opencode-go")
    if isinstance(entry, dict) and isinstance(entry.get("key"), str):
        return entry["key"]
    return None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every 3xx instead of re-sending the request to `Location`.

    HALF ONE OF THE EGRESS DEFECT. urllib's default opener FOLLOWS redirects,
    and it copies the original request's headers onto the new request - it does
    not strip `Authorization`. So the validated loopback feed endpoint could
    answer `302 Location: http://attacker.example/feed` and urllib would
    obediently re-send the POST off this box, carrying the feed bearer token,
    the X-Forwarded-User identity and the body. On the vendor GETs the same
    move hands out the household's Claude OAuth access token.

    Refusing is safe in the failure direction: a redirect becomes an
    "unavailable" gauge, which is the acceptance criterion rather than a
    regression. NEITHER THE TARGET NOR THE RESPONSE IS PUT IN THE MESSAGE - a
    hostile or confused server chooses that string, and the message ends up in
    the journal.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EgressRefused(
            "refused an HTTP %s redirect: this request may not be re-sent to "
            "a destination the response chose" % code)


def _peer_checked_connection(base_class, allow_host):
    """A connection class that asks the socket where it really landed.

    HALF TWO OF "re-validate the address actually connected to". The check runs
    inside `connect()`, after the TCP handshake and BEFORE http.client writes a
    single byte of the request line, so a connection to an address that is not
    on this box is dropped with the token and the body still unsent.
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
    knob accepts https, not because the bridge-only tracker is ever reached
    that way today."""

    def __init__(self, allow_host):
        urllib.request.HTTPSHandler.__init__(self)
        self._peer_checked = _peer_checked_connection(
            http.client.HTTPSConnection, allow_host)

    def https_open(self, req):
        return self.do_open(self._peer_checked, req, context=self._context)


def feed_opener(bridge_addresses=None):
    """The opener the FEED POST uses: no proxy, no redirect, no off-box peer.

    Three independent refusals, because validating the URL is not enough on its
    own - the URL says where we MEANT to go and two mechanisms could still send
    the request somewhere else:

      * `ProxyHandler({})` installs NO proxy rather than reading the
        environment. An `http_proxy` or `https_proxy` exported into the unit
        would otherwise route the POST - bearer token, identity header and body
        - through a LAN proxy that then sees all of it, defeating the whole
        "never leaves this box" property without touching the feed URL at all.
      * `_RefuseRedirects` refuses to be told where to go by the response.
      * the peer check asks the socket what address it actually reached and
        drops the connection before the request is written if that is not
        loopback or an address a local docker bridge is really carrying.

    `bridge_addresses` is injectable for the same reason it is in
    `is_local_destination`: a test states the box's bridges rather than
    inheriting whatever this machine happens to have.

    Implements: SR-021, LLR-005
    """
    def allow_host(host):
        return is_local_destination(host, bridge_addresses)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirects(),
        _LocalOnlyHTTPHandler(allow_host),
        _LocalOnlyHTTPSHandler(allow_host))


def vendor_opener():
    """The opener the VENDOR GETs use - and it is a DIFFERENT decision, taken
    deliberately rather than inherited from the feed's.

    These calls are OUTBOUND TO THE INTERNET BY DESIGN (api.anthropic.com,
    opencode.ai), so the feed's peer check would be nonsense here and is
    absent. The other two refusals still apply, and each was decided on its own
    merits:

      * REDIRECTS ARE REFUSED, because urllib carries `Authorization` across a
        cross-host redirect. A 302 from an impersonated or compromised vendor
        endpoint would hand the household's Claude OAuth access token, or the
        OpenCode workspace key, to whatever host the `Location` names. Both
        endpoints answered 200 DIRECTLY during the 2026-09-09 verification
        calls, so refusing costs nothing that has ever been observed, and the
        price if a vendor starts redirecting is an "unavailable" gauge - which
        is exactly the outcome this feeder is built to produce when it cannot
        read a source honestly.
      * NO PROXY IS INHERITED FROM THE ENVIRONMENT. An `http_proxy` exported
        box-wide would otherwise route a vendor bearer token through a LAN
        proxy that terminates TLS. There is deliberately NO knob to opt back
        in: this household's hub reaches the internet directly, and a proxy
        knob nobody needs is a second way for a token to leave. If an egress
        proxy is ever genuinely required it is a requirement change, not a
        setting.

    Implements: SR-021, LLR-005
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseRedirects())


def http_json(url, headers, timeout):
    """One GET, decoded. Every failure class becomes SourceFailure.

    Transport error, timeout, non-200 and an undecodable body are ONE outcome
    on purpose - see SourceFailure. The response body is NOT echoed into the
    exception on an auth failure, because a 401 body can carry request context.

    The URL must be https. These headers carry a vendor bearer token, and the
    URL is a knob (AI_USAGE_CLAUDE_URL / AI_USAGE_OPENCODE_URL); an http one
    would put the household's token on the wire in clear. The refusal is here
    rather than at the knob because this is the one function that attaches the
    token to a request.
    """
    if not url.lower().startswith("https://"):
        raise SourceFailure(
            "%s: refused - a vendor URL carrying a bearer token must be https"
            % url)
    request = urllib.request.Request(url, headers=headers)
    try:
        with vendor_opener().open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SourceFailure("%s: HTTP %s" % (url, response.status))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SourceFailure("%s: HTTP %s" % (url, exc.code))
    except (SourceFailure, EgressRefused):
        raise
    except Exception as exc:
        raise SourceFailure("%s: %s" % (url, type(exc).__name__))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SourceFailure("%s: body is not JSON" % url)


def read_codex(env):
    """Source 1: `codex app-server` JSON-RPC `account/rateLimits/read`.

    NO CREDENTIAL PASSES THROUGH THIS PROCESS. The child reads its own
    `~/.codex/auth.json`; we write two JSON-RPC frames to its stdin and read
    one result off its stdout. That is why codex is the source with no
    credential knob.
    """
    binary = env.get("AI_USAGE_CODEX_BIN") or "codex"
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    try:
        child = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as exc:
        raise SourceFailure("codex: cannot launch %s (%s)" % (binary, type(exc).__name__))

    result = {}
    deadline = time.time() + timeout

    def pump():
        for line in child.stdout:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("id") == 2:
                result["frame"] = frame
                return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        child.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "homehub-ai-usage",
                                      "version": "1"}}}) + "\n")
        child.stdin.flush()
        time.sleep(1.0)
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized",
                                      "params": None}) + "\n")
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2,
                                      "method": "account/rateLimits/read"}) + "\n")
        child.stdin.flush()
        while time.time() < deadline and "frame" not in result:
            time.sleep(0.2)
    except OSError as exc:
        raise SourceFailure("codex: app-server stdin closed (%s)" % type(exc).__name__)
    finally:
        try:
            child.kill()
        except OSError:
            pass

    frame = result.get("frame")
    if frame is None:
        raise SourceFailure("codex: no rateLimits answer within %ss" % timeout)
    if "error" in frame:
        raise SourceFailure("codex: JSON-RPC error %s" % frame["error"].get("code"))
    return parse_codex(frame.get("result"), cycle_now(env))


def read_claude(env):
    """Source 2: the OAuth usage endpoint, with its beta header and UA."""
    path = env.get("AI_USAGE_CLAUDE_CREDENTIALS")
    if not path:
        raise SourceFailure("claude: AI_USAGE_CLAUDE_CREDENTIALS unset")
    token = read_secret_file(path, _claude_token)
    version = env.get("AI_USAGE_CLAUDE_VERSION") or "2.0.0"
    url = env.get("AI_USAGE_CLAUDE_URL") or "https://api.anthropic.com/api/oauth/usage"
    body = http_json(url, {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/" + version,
        "Accept": "application/json",
    }, int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30))
    return parse_claude(body, cycle_now(env))


def read_opencode(env):
    """Source 3: the Zen usage endpoint, with the workspace bearer key."""
    path = env.get("AI_USAGE_OPENCODE_AUTH")
    if not path:
        raise SourceFailure("opencode: AI_USAGE_OPENCODE_AUTH unset")
    key = read_secret_file(path, _opencode_key)
    url = env.get("AI_USAGE_OPENCODE_URL") or "https://opencode.ai/zen/go/v1/usage"
    # THE USER AGENT IS LOAD-BEARING AND WAS FOUND BY RUNNING THIS, NOT BY
    # READING ANYTHING. The verification call of 2026-09-09 sent a named agent
    # and got 200; the first cut of this function sent none, so urllib's
    # default `Python-urllib/3.x` went out and the endpoint answered 403. The
    # feeder handled it correctly - two "unavailable" gauges and a named
    # failure, never a green one - which is exactly how the defect became
    # visible instead of silently posting nothing.
    body = http_json(url, {
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": env.get("AI_USAGE_AGENT") or "homehub-ai-usage/1",
    }, int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30))
    return parse_opencode(body, cycle_now(env))


SOURCE_READERS = {
    "codex": read_codex,
    "claude": read_claude,
    "opencode": read_opencode,
}


def enabled_sources(env):
    """Which of the three to read. Absent knob = all three; each is optional.

    A source whose credential the Owner has not supplied is switched OFF here
    rather than left to fail every cycle - a permanently unavailable gauge on
    the panel teaches people to ignore unavailable gauges.
    """
    raw = (env.get("AI_USAGE_SOURCES") or "codex,claude,opencode").strip()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in SOURCE_READERS]
    if unknown:
        raise SystemExit("REFUSED: AI_USAGE_SOURCES names unknown source(s): %s"
                         % ", ".join(unknown))
    return names


def post_gauge(body, url, env, timeout):
    """POST one gauge through the local-only opener. Returns (ok, detail).

    Never raises: a failed post is a reported failure, not a dead cycle, so the
    remaining gauges still go out.

    THE REMOTE'S OWN WORDS ARE NEVER PUT IN `detail`. The previous version
    returned `exc.read()[:200]`, which `main` prints to stderr and systemd
    writes to the journal - so anything a responding server or an interposed
    proxy chose to reflect, INCLUDING a bearer token echoed back in an error
    body, was persisted to disk by this feeder. The status code is ours to
    read; the body is the remote's to write, and it does not get a journal.

    Implements: SR-021, LLR-005
    """
    headers = {"Content-Type": "application/json"}
    token = env.get("AI_USAGE_FEED_TOKEN")
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


def run_cycle(env, now=None, readers=None, poster=None):
    """One feeder cycle: read every enabled source, post every gauge.

    Contract:
      Inputs:  env: the process environment (plus `_identity`); `readers` and
               `poster` are injectable so the tests can exercise every failure
               class without the network.
      Outputs: (posted: list of (key, fresh, ok), failures: list of str).

    A SOURCE FAILING IS NOT A CYCLE FAILING, AND NEITHER IS A GAUGE FAILING.
    Each source is read inside its own try, and the gauges belonging to a
    source that raised go down the unavailable path in `build_post` while the
    other sources still post live numbers - that is the whole reason the
    readers return dicts keyed by gauge id. Each gauge is then BUILT and POSTED
    inside its own try as well, which is the second half and was missing: a
    body this repo refuses to build (a vendor window we will not stand behind,
    a stored entry that is not history) used to raise out of the loop and end
    the cycle BEFORE the remaining gauges were posted, leaving their previously
    fresh values green on the panel until they expired. A gauge that cannot be
    built falls back to the never-measured sentinel - value 0, no stamp - so
    the failure still shows up as "unavailable" rather than as a hole.

    Implements: SR-021, LLR-005
    """
    now = int(now if now is not None else time.time())
    readers = readers or SOURCE_READERS
    state_path = env.get("AI_USAGE_STATE_FILE") or "/var/lib/homehub-ai/usage-state.json"
    state_root = resolve_state_root(env)
    feed_url = env["_feed_url"]
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    active = enabled_sources(env)
    # One clock for the whole cycle: the parsers check each vendor window
    # against the same `now` the gauge will be stamped with (see `cycle_now`).
    env["_now"] = now

    readings, failures = {}, []
    for name in active:
        try:
            readings.update(readers[name](env))
        except SourceFailure as exc:
            failures.append("%s: %s" % (name, exc))
        except Exception as exc:               # a reader bug is a source failure
            failures.append("%s: unexpected %s" % (name, type(exc).__name__))

    state = load_state(state_path, now)
    posted = []
    for spec in GAUGE_SPECS:
        if spec.source not in active:
            continue
        reading = readings.get(spec.key)
        try:
            body, fresh = build_post(spec, reading, state.get(spec.key), now)
        except (ValueError, SourceFailure) as exc:
            failures.append("gauge %s: %s" % (spec.key, exc))
            body, fresh = build_gauge(spec, 0.0, None, None, None), False
            reading = None
        try:
            ok, detail = (poster or post_gauge)(body, feed_url, env, timeout)
        except Exception as exc:               # a poster bug is a failed post
            ok, detail = False, type(exc).__name__
        posted.append((spec.key, fresh, ok))
        if not ok:
            failures.append("post %s: %s" % (spec.key, detail))
        if reading is not None:
            state[spec.key] = {"value": reading.percent, "observed_at": now,
                               "window_end": reading.window_end,
                               "window_seconds": reading.window_seconds}
    try:
        save_state(state, state_path, state_root)
    except OSError as exc:
        # Including PermissionError from the write guard. The gauges are
        # already posted; losing the history is a named failure, not a crash.
        failures.append("state %s: %s" % (state_path, type(exc).__name__))
    return posted, failures


def main(argv=None):
    """Entry point: gate, resolve, one cycle, report. It does not compute.

    Implements: SR-021
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    if not resolve_enabled(env):
        print("ai-usage: AI_USAGE_ENABLED is not true - nothing to do.")
        return 0
    env["_identity"] = resolve_identity(env)
    env["_feed_url"] = resolve_feed_url(env)
    if "--check" in argv:
        print("ai-usage: configuration accepted (identity set, destination local).")
        return 0
    posted, failures = run_cycle(env)
    for key, fresh, ok in posted:
        print("ai-usage: %-32s %-12s %s" % (key, "live" if fresh else "UNAVAILABLE",
                                            "posted" if ok else "POST FAILED"))
    for line in failures:
        print("ai-usage: %s" % line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
