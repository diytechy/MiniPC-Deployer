#!/usr/bin/env python3
"""The hub's sleep feeder: the ResMed myAir sleep score -> one NagLight gauge.

ONE RESPONSIBILITY: read myAir's OWN nightly sleep score for the household's
CPAP and post it to NagLight as the `sleep` gauge. Nothing is computed here -
no formula, no component, no colour. NagLight owns severity and colour; this
file only decides WHICH NIGHT'S NUMBER (if any) is true right now, and when it
became true.

WHERE THE DESIGN COMES FROM: docs/GAUGE_RINGS_AND_SLEEP_PLAN_2026-10-11.md in
the HomeHub repo (revision 5), sections 3.6 (a dead login goes grey), 4.2-4.5
(the source, the feeder, THE NIGHTLY DATA CONTRACT and the wire). The S0b probe
of 2026-10-11 settled the vendor facts this file depends on (README.md).

THE CLIENT FLOW IS A RE-IMPLEMENTATION, WITH ATTRIBUTION. The sign-in sequence
(Okta authn -> authorization code with PKCE -> token, then AppSync GraphQL)
follows the MIT-licensed `prestomation/resmed_myair_sensors` Home Assistant
integration at commit e2c52a30 (custom_components/resmed_myair/client/: auth.py,
graphql.py, rest_client.py, regions.py). It is rewritten here on the Python
STANDARD LIBRARY ONLY, because every other feeder is stdlib-only and the hub's
system Python has no ensurepip, so a venv would be a new moving part for four
HTTP calls. The upstream licence text is reproduced in README.md.

THE FIVE PROPERTIES THIS FILE EXISTS TO KEEP, each with the symbol that holds it:
  1. never a manufactured zero, never an old night shown as last night
                                       -> `valid_score`, `decide_night`
  2. `observed_at` is frozen at first publication and never later than 12:00
     local on the night-key date (a future stamp reads as STALE in NagLight)
                                       -> `upsert_nights`
  3. a dead login goes grey after two cycles and stays grey
                                       -> `run_cycle` (AUTH_* below)
  4. the journal carries status only, never a score or any health value
                                       -> every message in this file
  5. no automated reading of anyone's email, ever
                                       -> there is no mail code in this file

Implements: SR-070, SR-071, SR-072
"""

import base64
import datetime as dt
import hashlib
import http.client
import http.cookies
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

# ── The wire contract (NagLight IF-012 / this repo's IF-013) ─────────────────
# Plan 4.5. These are not tunables:
#   * unit "pts" is not "%", so the gauge lands in the life row, not the AI row;
#   * min/max/target are PRESENT (an omitted number used to be stored as a
#     fabricated 0), and the colour ladder is NagLight's, from target 80 and
#     favourable "high";
#   * window {"kind": "daily"} with NO start, end or direction is deliberate:
#     NagLight's Validate refuses only a half window, Window.Empty() ignores
#     `kind`, and Stale() reads StaleAfter("daily") = 36 h. A direction here
#     would be refused (a standing line has no direction);
#   * NO previous_value: NagLight derives the trend baseline itself.
GAUGE_ID = "sleep"
GAUGE_LABEL = "Sleep"
GAUGE_ICON = "sleep"
GAUGE_UNIT = "pts"
GAUGE_MIN = 0
GAUGE_MAX = 100
GAUGE_TARGET = 80
GAUGE_FAVOURABLE = "high"
GAUGE_WINDOW_KIND = "daily"
RUNE_LIMIT = 120

SOURCE = "cpap-myair"

# THE DAILY HORIZON (NagLight StaleAfter("daily")). Kept here only so the timer
# cadence can be tested against it; the feeder never computes staleness.
DAILY_STALE_SECONDS = 36 * 3600

# Plan 4.4: from this LOCAL hour, "no record for the expected night" stops
# meaning "cloud lag" and starts meaning "unavailable".
GRACE_HOUR_LOCAL = 14
# Noon on the night-key date is the stamp a night gets when it is published
# late. Plan 4.4: the EARLIER of this and the time of first publication.
STAMP_HOUR_LOCAL = 12
# Only these two night keys can ever be shown (today's, and before the grace
# hour yesterday's), so only these are ever stored; the rest of what myAir
# returns for 30 days is not kept. State is pruned to this many days back.
KEEP_NIGHTS_BACK_DAYS = 3
MYAIR_WINDOW_DAYS = 30

# THE HUB AND THE VENDOR DO NOT SHARE A CLOCK. Small on purpose: it absorbs NTP
# jitter, it does not make a future stamp look real.
MAX_CLOCK_SKEW_SECONDS = 300
# No stamp this feeder can write predates the block that wrote it (2025-01-01).
EPOCH_FLOOR = 1735689600

DEFAULT_STATE_ROOT = "/var/lib/homehub-sleep"
DEFAULT_STATE_NAME = "sleep-state.json"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_BODY_BYTES = 4 * 1024 * 1024        # a month of nightly records is a few KB

# ── State-file keys ──────────────────────────────────────────────────────────
# The state file is {"nights": {...}, "__auth_failures__": {...},
# "__auth_failed__": {...}}. Each part has its OWN validator in `load_state` -
# never an exemption from validation - because this is a one-shot unit and
# whatever the loader does not recognise is erased by the next `save_state`.
#
# "nights" holds what was FETCHED or staged (score + frozen stamp); "published"
# holds the night keys the feeder actually POSTED as a score (first-publication
# epoch). They are separate on purpose: "before 14:00 the previous night's score
# stays" means a night that was really on the wall, not one that merely sits in
# the cache after an outage (Sol #1). "schedule" remembers when myAir was last
# asked, so a one-shot unit can keep to the fetch windows below.
NIGHTS_KEY = "nights"
PUBLISHED_KEY = "published"
SCHEDULE_KEY = "schedule"
AUTH_COUNTS_KEY = "__auth_failures__"
AUTH_MARKER_KEY = "__auth_failed__"
AUTH_CONFIRM_CYCLES = 2         # consecutive auth-failed cycles before the gauge greys.
_AUTH_COUNT_CEILING = 100       # a stored count above this is corruption, not history.

# ── When myAir is asked (the timer ticks every 30 min; the NETWORK does not) ──
# Every tick posts from stored state. A sign-in is a full login to a health
# vendor with no refresh token, so it happens only:
#   * while the expected night is not yet stored, between 06:00 and 16:00 local,
#     at most once per MISSING_RETRY_SECONDS;
#   * once the night IS stored, for a correction re-fetch from 14:30 local and
#     another from 20:00 local (each at most once per local day);
#   * one more time, on the very next tick, after a FIRST authentication failure
#     (so the two-cycle confirmation takes ~30 minutes, not hours);
#   * after a CONFIRMED authentication failure, only once the back-off below has
#     elapsed (a wrong password at 48 tries a day can lock the account, the app
#     included) - or on `--retry-now`.
# A normal day is therefore roughly 4-6 sign-ins, not 48.
FETCH_OPEN_HOUR_LOCAL = 6
FETCH_CLOSE_HOUR_LOCAL = 16
MISSING_RETRY_SECONDS = 120 * 60
# The timer is 30 min plus up to 120 s of RandomizedDelay: a spacing check that
# demanded the full interval would round up to the next tick every time.
SPACING_SLACK_SECONDS = 5 * 60
CORRECTION_SLOTS = (("pm", 14, 30), ("eve", 20, 0))
# A slot (the 14:30 / 20:00 fetches) is marked DONE only after a read that did not
# fail. A transient (non-auth) failure may be retried: up to SLOT_MAX_ATTEMPTS
# attempts in all (the first plus two more), SLOT_RETRY_SPACING_SECONDS apart,
# none started at or after SLOT_RETRY_END_HOUR_LOCAL. The auth back-off overrides all.
SLOT_MAX_ATTEMPTS = 3
SLOT_RETRY_SPACING_SECONDS = 30 * 60
SLOT_RETRY_END_HOUR_LOCAL = 22
# A back-off recovery attempt (an automatic one, not --retry-now) is made only in
# this local window, so a recovery never lands at 00:30 and never keeps a person up.
RECOVERY_OPEN_HOUR_LOCAL = 6
RECOVERY_CLOSE_HOUR_LOCAL = 22
# The expected night is never FIRST published before this local hour. NagLight's
# tracking day starts at 02:00, so a stamp frozen at 00:30 would land in
# yesterday's tracking day. The record is staged and its stamp floored here.
FIRST_PUBLISH_HOUR_LOCAL = 6
# A published night key later than today + this many days is corruption or a
# forward clock jump, and is dropped on load with a status line.
PUBLISHED_AHEAD_DAYS = 1
# systemd-timesyncd touches this file once the clock has been synchronised.
SYNC_FLAG_PATH = "/run/systemd/timesync/synchronized"
AUTH_BACKOFF_SECONDS = 6 * 3600
AUTH_BACKOFF_LOCKED_SECONDS = 24 * 3600
LOCKED_OUT = "LOCKED_OUT"
_CODE_SHAPE = re.compile(r"^[A-Za-z_]{1,24}$")

MFA_MESSAGE = ("myAir now asks for an emailed code - a person must sign in "
               "(see refresh-gauge-logins)")

# Okta's `status` words, compared against this list. A status NOT in it is
# reported as UNRECOGNISED and never echoed: the response is the remote's to
# write and it does not get a journal.
_OKTA_STATUSES = frozenset({
    "LOCKED_OUT", "PASSWORD_EXPIRED", "PASSWORD_WARN", "PASSWORD_RESET",
    "RECOVERY", "RECOVERY_CHALLENGE", "MFA_ENROLL", "MFA_ENROLL_ACTIVATE",
    "MFA_CHALLENGE", "UNAUTHENTICATED", "SUCCESS", "MFA_REQUIRED",
})

_DATE_SHAPE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COUNTRY_SHAPE = re.compile(r"^[A-Za-z0-9]{1,16}$")


# ── Exceptions ───────────────────────────────────────────────────────────────

class SourceFailure(Exception):
    """myAir could not be read, or answered with something unusable.

    Every message in this file that goes into one is OUR OWN TEXT. The remote's
    words (an Okta error summary, a GraphQL message) are never put in one: they
    end up in the journal, and a remote can reflect a request back.
    """


class AuthFailure(SourceFailure):
    """A SourceFailure that is KNOWN to be an authentication failure (plan 3.6).

    Raised for an Okta HTTP 401, for any authn `status` other than SUCCESS
    (wrong password, locked, expired, MFA required), and for a GraphQL 401 or
    `unauthorized` after a sign-in that succeeded. It IS a SourceFailure, so
    every handler that does not care still works; `run_cycle` catches it FIRST.

    NO CODE MAY CATCH ONE AND RE-RAISE IT AS A PLAIN SourceFailure: that would
    turn a dead login back into "a network blip" and the gauge would never
    grey. `status` is the HTTP status and `code` an allow-listed word.

    Implements: SR-072, LLR-1088
    """

    def __init__(self, message, status=None, code=None):
        SourceFailure.__init__(self, message)
        self.status = status
        self.code = code


class MfaRequired(AuthFailure):
    """Okta answered MFA_REQUIRED: myAir now wants an emailed code.

    A person has to sign in; this feeder NEVER reads a mailbox. It goes through
    the same two-cycle confirmation as any other authentication failure.

    Implements: SR-072, LLR-1090
    """


class EgressRefused(Exception):
    """A request tried to leave this box by a route the feeder does not permit."""


# ── Pure core: no I/O below this line until the SHELL banner ────────────────

def parse_night_date(text):
    """A strict bare `YYYY-MM-DD` -> datetime.date, or None for anything else.

    Strict on purpose: myAir's `startDate` is a bare date, and a datetime
    string, a short year or a US-style date must be "unreadable", not guessed.
    """
    if not isinstance(text, str):
        return None
    match = _DATE_SHAPE.match(text)
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def night_key_for(start_date):
    """The NIGHT KEY for a myAir record: `startDate` + 1 calendar day.

    S0b settled this on 2026-10-11 against the Owner's real account: `startDate`
    is the evening the night BEGAN, and the Owner confirmed in the app that the
    record dated 2026-10-10 is the night that ended on the morning of
    2026-10-11. The night key is the local date on which the night ENDED.

    CALENDAR arithmetic on dates, so a DST change cannot move it.

    Implements: SR-071, LLR-1078
    """
    return start_date + dt.timedelta(days=1)


def valid_score(raw):
    """myAir's own score as a number, or None when it is not a usable score.

    Contract: a finite int or float within 0..100 INCLUDING 0 (myAir's own zero
    is a score). A null, a missing key, a string, a bool, NaN, an infinity or an
    out-of-range number is "no data" - NEVER 0. `bool` is excluded because
    `True` is an `int` in Python and would otherwise publish as 1.

    Returns an int when the number is whole (82.0 -> 82), else the float.

    Implements: SR-071, LLR-1079
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    if raw < GAUGE_MIN or raw > GAUGE_MAX:
        return None
    if isinstance(raw, float) and raw == int(raw):
        return int(raw)
    return raw


def local_now(now, tz):
    """The instant `now` (epoch seconds) as an aware datetime in `tz`."""
    return dt.datetime.fromtimestamp(int(now), tz)


def expected_night(now, tz):
    """The expected night key: TODAY'S LOCAL DATE (plan 4.4).

    Implements: SR-071, LLR-1082
    """
    return local_now(now, tz).date()


def past_grace(now, tz):
    """True from 14:00 LOCAL. 13:59 is before it, 14:00 is the first moment after.

    Read off the local clock via the zone's own offset for that instant, so a
    DST change day (a 23- or 25-hour day) cannot shift it.

    Implements: SR-071, LLR-1082
    """
    return local_now(now, tz).hour >= GRACE_HOUR_LOCAL


def noon_epoch(day, tz):
    """Epoch seconds of 12:00 LOCAL on the calendar date `day`.

    Built from the date plus the zone, then converted through the zone's offset
    FOR THAT DATE: never by adding hours to some other instant, which is what
    goes wrong on a day with 23 or 25 hours. Noon is never inside a DST
    transition in any real zone.

    Implements: SR-071, LLR-1081, LLR-1084
    """
    return int(dt.datetime(day.year, day.month, day.day, STAMP_HOUR_LOCAL, 0, 0,
                           tzinfo=tz).timestamp())


def rfc3339_local(epoch_seconds, tz):
    """Epoch -> RFC3339 WITH the zone's offset for that instant (plan 4.5).

    The offset is the one in force at that instant, so a stamp on either side
    of a DST change carries the right one.
    """
    return dt.datetime.fromtimestamp(int(epoch_seconds), tz).isoformat(timespec="seconds")


def first_publish_epoch(day, tz):
    """Epoch seconds of FIRST_PUBLISH_HOUR_LOCAL (06:00) local on the date `day`.

    Implements: SR-071, LLR-1081
    """
    return int(dt.datetime(day.year, day.month, day.day, FIRST_PUBLISH_HOUR_LOCAL, 0, 0,
                           tzinfo=tz).timestamp())


Record = namedtuple("Record", ("start_date", "score"))


def parse_records(payload):
    """GraphQL `getPatientWrapper.sleepRecords.items` -> ([Record], unreadable_count).

    Only `startDate` and `sleepScore` are read; nothing else is requested, held
    or logged. A record whose `startDate` is not a bare date is counted and
    skipped (we cannot key it); its score is NOT parsed here, `valid_score`
    judges it in `upsert_nights`.

    Raises SourceFailure for a response that is not shaped like the S0b-observed
    one: an absent `data`, `getPatientWrapper`, `sleepRecords` or `items` is not
    "no records", it is a response we do not understand, and an empty list is
    the only honest "none".
    """
    if not isinstance(payload, dict):
        raise SourceFailure("graphql: the response is not an object")
    data = payload.get("data")
    wrapper = data.get("getPatientWrapper") if isinstance(data, dict) else None
    records = wrapper.get("sleepRecords") if isinstance(wrapper, dict) else None
    items = records.get("items") if isinstance(records, dict) else None
    if not isinstance(items, list):
        raise SourceFailure("graphql: no sleepRecords.items in the response")
    out, unreadable = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise SourceFailure("graphql: a sleep record is not an object")
        start = parse_night_date(item.get("startDate"))
        if start is None:
            unreadable += 1
            continue
        out.append(Record(start, item.get("sleepScore")))
    return out, unreadable


def upsert_nights(nights, records, now, tz):
    """Fold one fetch into the stored nights. Returns the changed night keys.

    THE CONTRACT, line by line (plan 4.4):
      * ONE ENTRY PER NIGHT KEY, upserted. The key is `startDate` + 1 day.
      * Only the EXPECTED night and the PREVIOUS night are ever kept: they are
        the only two that can be shown. A record for a night key in the future
        is ignored (it can neither publish nor block today's).
      * A record with no valid score changes NOTHING: it neither creates an
        entry nor erases a published one. "No data" is not a score and is not
        a correction to nothing.
      * A NEW night is stamped `min(12:00 local on the night-key date, now)`:
        the EARLIER of the two. A record that arrives at 07:00 is stamped
        07:00, because NagLight treats an `observed_at` in the future as STALE
        and a 12:00 stamp would show grey until noon. A record that arrives
        late is stamped noon, never the later fetch time.
      * A CORRECTION to an existing night changes the value ONLY. `observed_at`
        is frozen at first sight and no later fetch moves it, so a revised
        night never looks fresher than it is.
      * Two records for one night with DIFFERENT valid scores are ambiguous and
        are skipped for this cycle, rather than choosing one.

    Implements: SR-071, LLR-1079, LLR-1080, LLR-1081
    """
    today = expected_night(now, tz)
    wanted = (today, today - dt.timedelta(days=1))
    seen = {}
    for record in records:
        key = night_key_for(record.start_date)
        if key not in wanted:
            continue
        score = valid_score(record.score)
        if score is None:
            continue
        if key in seen and seen[key] != score:
            seen[key] = None                  # ambiguous: two scores for one night
        else:
            seen[key] = score
    changed = []
    for key, score in seen.items():
        if score is None:
            continue
        name = key.isoformat()
        entry = nights.get(name)
        if entry is None:
            stamp = min(noon_epoch(key, tz), int(now))
            if key == today:
                # NO FIRST PUBLICATION OF THE EXPECTED NIGHT BEFORE 06:00 local. The
                # record is staged now, but its frozen stamp is floored at 06:00 on
                # its own date: a stamp in the future is QUARANTINED (never
                # displayed) until the clock reaches it, so the night is first
                # published at 06:00 and its stamp lands in the right NagLight
                # tracking day (which starts at 02:00).
                stamp = max(stamp, first_publish_epoch(key, tz))
            nights[name] = {"score": score, "observed_at": stamp}
            changed.append(name)
        elif entry["score"] != score:
            entry["score"] = score            # a correction: value only, stamp frozen
            changed.append(name)
    return changed


def decide_night(nights, published, now, tz):
    """Which stored night, if any, is the one to show. Returns a night key or None.

    THE UNAVAILABLE RULES (plan 4.4), in one place:
      * The EXPECTED night (today's local date) wins whenever it is there.
      * BEFORE 14:00 local, with no record for the expected night, the PREVIOUS
        night's score stays and ages normally - but only a night the feeder
        actually PUBLISHED (`published`), not one that merely sits in the cache
        because a fetch after an outage or a dead-login period returned it. A
        never-published previous night is not "the score that stays": it was
        never on the wall. Anything older than the previous night is never shown.
      * FROM 14:00 local, with no record for the expected night, the answer is
        None: UNAVAILABLE. This is also the rule "a newest record that is not
        for the expected night posts unavailable rather than a score" - an old
        night is never shown as last night.
      * Cloud lag never paints a false zero: None means "no score", and the
        caller posts the stale-on-arrival body, not a 0 reading.

    Implements: SR-071, LLR-1082, LLR-1083
    """
    today = expected_night(now, tz)
    key = today.isoformat()
    entry = nights.get(key)
    if entry is not None and entry["observed_at"] <= int(now):
        return key
    # QUARANTINE: an entry stamped in the future is not displayed (NagLight would
    # paint it stale). And ONCE TODAY'S NIGHT HAS BEEN PUBLISHED, NO EARLIER NIGHT
    # IS EVER SELECTED FOR DISPLAY THAT DAY: if today's record is unavailable (a
    # same-day backward clock step quarantined it) the answer is unavailable, not
    # yesterday's score under today's date.
    if key in published:
        return None
    if not past_grace(now, tz):
        previous = (today - dt.timedelta(days=1)).isoformat()
        entry = nights.get(previous)
        if entry is not None and previous in published and entry["observed_at"] <= int(now):
            return previous
    return None


def build_gauge(score, observed_at):
    """The wire body (plan 4.5). `observed_at` is an RFC3339 string or None.

    `score` None is the never-measured body: value 0 and NO `observed_at`, which
    NagLight treats as stale on arrival. The gauge EXISTS so the panel can say
    "unavailable" rather than show nothing; the 0 is never rendered as a live
    reading, and it is the one number in this file nobody measured.

    Implements: SR-070, LLR-1077
    """
    body = {
        "kind": "gauge",
        "id": GAUGE_ID,
        "label": GAUGE_LABEL,
        "icon": GAUGE_ICON,
        "unit": GAUGE_UNIT,
        "value": 0 if score is None else score,
        "min": GAUGE_MIN,
        "max": GAUGE_MAX,
        "target": GAUGE_TARGET,
        "favourable": GAUGE_FAVOURABLE,
        "window": {"kind": GAUGE_WINDOW_KIND},
    }
    if score is not None and observed_at:
        body["observed_at"] = observed_at
    for field in ("id", "label", "icon", "unit"):
        if len(body[field]) > RUNE_LIMIT:                  # pragma: no cover - constants
            raise ValueError("gauge %s exceeds %d runes" % (field, RUNE_LIMIT))
    return body


def validate_night_entry(key, entry, now, tz):
    """A stored night, or None if it is not credible. Its own validator.

    A stamp later than 12:00 local on the night-key date would be a night that
    looks fresher than it can be; a stamp before the block was written is
    corruption. A stamp in the FUTURE is NOT dropped: it is a real observation
    (the host clock stepped back, or the night was staged for 06:00) and
    destroying it could make an earlier night display in its place. It is
    quarantined by `decide_night` - never displayed - until the clock passes it.
    (`now` is kept in the signature for the other validators' sake.)
    """
    day = parse_night_date(key)
    if day is None or not isinstance(entry, dict):
        return None
    score = valid_score(entry.get("score"))
    stamp = entry.get("observed_at")
    if score is None or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    if isinstance(stamp, float) and not math.isfinite(stamp):
        return None
    stamp = int(stamp)
    if stamp < EPOCH_FLOOR:
        return None
    if stamp > noon_epoch(day, tz):
        return None
    return {"score": score, "observed_at": stamp}


def validate_auth_counts(entry):
    """The consecutive-auth-failure counter, or {} if it is not credible.

    Contract: {SOURCE: int in 1..100}. An unknown source, a zero, a bool, a
    float or a string is dropped, never repaired.

    Implements: SR-072, LLR-1089
    """
    if not isinstance(entry, dict):
        return {}
    count = entry.get(SOURCE)
    if isinstance(count, bool) or not isinstance(count, int):
        return {}
    return {SOURCE: count} if 1 <= count <= _AUTH_COUNT_CEILING else {}


def validate_auth_marker(entry, now):
    """The sticky "auth failed" marker {"auth_failed": true, "at": epoch}, or None.

    Implements: SR-072, LLR-1090
    """
    if not isinstance(entry, dict) or entry.get("auth_failed") is not True:
        return None
    stamp = entry.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) \
            or not math.isfinite(stamp):
        return None
    stamp = int(stamp)
    if stamp < EPOCH_FLOOR or stamp > int(now) + MAX_CLOCK_SKEW_SECONDS:
        return None
    marker = {"auth_failed": True, "at": stamp}
    code = entry.get("code")
    if isinstance(code, str) and _CODE_SHAPE.match(code):
        marker["code"] = code            # an allow-listed status word; LOCKED_OUT lengthens the back-off
    return marker


def validate_published(entry, now, tz):
    """({night key: first-publication epoch}, dropped_future_keys).

    Drops every entry not credible. A KEY later than today + PUBLISHED_AHEAD_DAYS
    is corruption or a forward clock jump: left in, `clock_went_backwards` would
    refuse every cycle until the calendar caught up, so it is dropped (and
    counted, for a status line). A key of tomorrow is kept: one day back is a
    real backwards step and must be seen.

    Implements: SR-071, LLR-1083
    """
    out, dropped = {}, 0
    if not isinstance(entry, dict):
        return out, dropped
    ceiling = expected_night(now, tz) + dt.timedelta(days=PUBLISHED_AHEAD_DAYS)
    for key, stamp in entry.items():
        day = parse_night_date(key)
        if day is None or isinstance(stamp, bool) \
                or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            continue
        if day > ceiling:
            dropped += 1
            continue
        stamp = int(stamp)
        # NO CEILING against `now`, deliberately: if the host clock has gone
        # BACKWARDS, every honest publication stamp is "in the future", and
        # dropping them here would erase the very evidence `clock_went_backwards`
        # needs. Only a stamp from before the block existed is corruption.
        if stamp >= EPOCH_FLOOR:
            out[key] = stamp
    return out, dropped


def validate_schedule(entry, now):
    """{"day", "attempt_at", "done"} or None. The memory of the fetch windows.

    Implements: SR-072, LLR-1086
    """
    if not isinstance(entry, dict) or parse_night_date(entry.get("day")) is None:
        return None
    stamp = entry.get("attempt_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        return None
    stamp = int(stamp)
    if not EPOCH_FLOOR <= stamp <= int(now) + MAX_CLOCK_SKEW_SECONDS:
        return None
    done = entry.get("done")
    names = [name for name, _h, _m in CORRECTION_SLOTS]
    if not isinstance(done, list) or any(d not in names for d in done):
        return None
    fails = {}
    raw = entry.get("fails", {})
    if isinstance(raw, dict):
        for name, count in raw.items():
            if name in names and not isinstance(count, bool) and isinstance(count, int) \
                    and 1 <= count <= 20:
                fails[name] = count
    return {"day": entry["day"], "attempt_at": stamp, "done": sorted(set(done)), "fails": fails}


def empty_state():
    # "_notes" is transient: status lines the loader wants journalled. Never saved.
    return {NIGHTS_KEY: {}, PUBLISHED_KEY: {}, SCHEDULE_KEY: None,
            AUTH_COUNTS_KEY: {}, AUTH_MARKER_KEY: None, "_notes": []}


def prune_nights(state, now, tz):
    """Drop nights and publications older than KEEP_NIGHTS_BACK_DAYS, and any dated
    later than tomorrow. Nothing older can be shown; a far-future key is corruption
    or a forward clock jump (the published set drops those on load too).

    Returns how many far-future NIGHT keys were dropped, for a status line.
    """
    today = expected_night(now, tz)
    oldest = today - dt.timedelta(days=KEEP_NIGHTS_BACK_DAYS)
    newest = today + dt.timedelta(days=PUBLISHED_AHEAD_DAYS)
    future = 0
    for part in (NIGHTS_KEY, PUBLISHED_KEY):
        for key in list(state[part]):
            day = parse_night_date(key)
            if day is None or day < oldest:
                del state[part][key]
            elif day > newest:
                del state[part][key]
                future += part == NIGHTS_KEY
    return future


def clock_went_backwards(state, now, tz):
    """True when the local date is EARLIER than the newest night already published.

    A night key is a local date and a published one cannot be in the future, so
    this only happens if the host clock has gone backwards (a bad RTC, an NTP
    step). Selecting or publishing then could stamp a "first publication" in the
    past or show the wrong night, so the cycle does neither (Sol #4). The feeder
    and NagLight share one host clock; skew BETWEEN clocks is out of scope.

    Implements: SR-071, LLR-1084
    """
    newest = max((parse_night_date(key) for key in state[PUBLISHED_KEY]), default=None)
    return newest is not None and expected_night(now, tz) < newest


def backoff_seconds(marker):
    """How long a confirmed authentication failure keeps the feeder off Okta.

    6 h, or 24 h when the failing authn status was LOCKED_OUT. Hammering a wrong
    or expired password at every tick (48 a day) is what locks a real account,
    app included.

    Implements: SR-072, LLR-1090
    """
    return AUTH_BACKOFF_LOCKED_SECONDS if marker.get("code") == LOCKED_OUT else AUTH_BACKOFF_SECONDS


def fetch_due(state, now, tz):
    """Decide whether this tick may call myAir. Returns (due, why, slots_to_mark).

    PURE: reads the state and the clock, touches nothing. See the block comment
    above FETCH_OPEN_HOUR_LOCAL for the schedule; the order below is its priority.
    `why` is a status word for the journal.

    Implements: SR-072, LLR-1086, LLR-1090
    """
    local = local_now(now, tz)
    marker = state[AUTH_MARKER_KEY]
    if marker is not None:
        # CONFIRMED dead login: nothing else matters until the back-off is over,
        # and then a recovery attempt is made only between 06:00 and 22:00 local.
        if now < marker["at"] + backoff_seconds(marker):
            return False, "back-off in force after a confirmed sign-in failure", []
        if not RECOVERY_OPEN_HOUR_LOCAL <= local.hour < RECOVERY_CLOSE_HOUR_LOCAL:
            return False, "back-off elapsed, waiting for the 06:00-22:00 recovery window", []
        return True, "back-off elapsed", []
    if state[AUTH_COUNTS_KEY].get(SOURCE, 0) >= 1:
        return True, "confirming a sign-in failure", []
    expected = local.date()
    schedule = state[SCHEDULE_KEY]
    today = schedule if schedule is not None and schedule["day"] == expected.isoformat() else None
    done = set(today["done"]) if today is not None else set()
    fails = today["fails"] if today is not None else {}
    stored = expected.isoformat() in state[NIGHTS_KEY]
    reached = [name for name, hour, minute in CORRECTION_SLOTS
               if (local.hour, local.minute) >= (hour, minute)]
    if not stored:
        if FETCH_OPEN_HOUR_LOCAL <= local.hour < FETCH_CLOSE_HOUR_LOCAL:
            if today is not None and now - today["attempt_at"] < MISSING_RETRY_SECONDS - SPACING_SLACK_SECONDS:
                return False, "asked recently", []
            return True, "expected night not stored yet", []
        # LATE-UPLOAD CATCH-UP: only the evening slot runs with the night missing -
        # one attempt a day, one extra sign-in on a late day - so a night uploaded
        # between 16:00 and 20:00 is fetched and published that evening. One read
        # covers (marks) every slot already reached.
        evening = CORRECTION_SLOTS[-1][0]
        if evening not in reached or evening in done:
            return False, "outside the 06:00-16:00 window", []
        pending, marks = [evening], reached
        reason = "late-upload catch-up (evening slot)"
    else:
        pending = [name for name in reached if name not in done]
        if not pending:
            return False, "expected night stored, no correction due", []
        marks, reason = pending, "correction re-fetch"
    # A slot is DONE only after a read that did not fail, so a transient failure
    # does not consume it; it may be retried, bounded in number, spacing and hour.
    failed = fails.get(pending[-1], 0)
    if failed == 0:
        return True, reason, marks
    if failed >= SLOT_MAX_ATTEMPTS:
        return False, "slot retries exhausted", []
    if local.hour >= SLOT_RETRY_END_HOUR_LOCAL:
        return False, "slot retry window (22:00) is over", []
    if now - today["attempt_at"] < SLOT_RETRY_SPACING_SECONDS - SPACING_SLACK_SECONDS:
        return False, "retrying a failed slot shortly", []
    return True, "retrying a failed read", marks


def clock_synchronized(exists=None, runner=None):
    """(True|False|None, method): has the host clock been synchronised?

    Night keys, the noon stamp and the 14:00 rule are LOCAL dates read off the
    host clock, and `After=time-sync.target` alone does not mean the clock is set.
    Two signals, stdlib only:
      1. systemd-timesyncd's flag file (`SYNC_FLAG_PATH`) - present once it has
         synchronised. This is what the hub (Ubuntu, systemd-timesyncd) provides.
      2. otherwise `timedatectl show -p NTPSynchronized --value` (5 s timeout):
         `yes` is synchronised, `no` is not.
    If neither is available the answer is None ("unknown"), which the cycle treats
    as NOT synchronised: it fetches nothing and posts nothing new.
    `exists` and `runner` are injectable for the tests.

    Implements: SR-071, LLR-1084
    """
    exists = exists or os.path.exists
    runner = runner or subprocess.run
    if exists(SYNC_FLAG_PATH):
        return True, "systemd-timesyncd flag file"
    try:
        out = runner(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None, "no synchronisation signal available"
    if out.returncode != 0:
        return None, "timedatectl could not answer"
    answer = (out.stdout or "").strip().lower()
    if answer == "yes":
        return True, "timedatectl NTPSynchronized=yes"
    if answer == "no":
        return False, "timedatectl NTPSynchronized=no"
    return None, "timedatectl gave an unreadable answer"


def retry_now_refusal(state_path, uid=None, owner_of=None):
    """A one-line reason `--retry-now` must not run, or None.

    It must run as the SERVICE account: as root it would write a root-owned state
    file the unit (User=homehub-sleep) can then never replace. Refused when the
    effective uid is 0, or when the state file exists and belongs to someone else.
    (No geteuid on Windows: the dev PC has no such account to protect.)
    """
    if uid is None:
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            return None
        uid = geteuid()
    if uid == 0:
        return ("--retry-now must not run as root: it would leave a root-owned state file "
                "the service account cannot replace; run it as the service account "
                "(see the README systemd-run line)")
    owner_of = owner_of or (lambda path: os.stat(path).st_uid)
    try:
        owner = owner_of(state_path)
    except OSError:
        return None
    if owner != uid:
        return ("--retry-now refused: the state file is owned by another user; run it as "
                "the account that owns it (see the README systemd-run line)")
    return None


# ── Configuration (pure: reads the env dict it is given) ────────────────────

def resolve_enabled(env):
    """True only for a literal `true`. Off is the default and every typo.

    Implements: SR-070, LLR-1075
    """
    return (env.get("SLEEP_ENABLED") or "false").strip() == "true"


def resolve_publish(env):
    """True unless SLEEP_PUBLISH is an explicit off word. DEFAULT IS ON.

    Plan 4.3 / decision 5: the score is on the wall from the day the feeder is
    deployed; `0` is an operator off switch and NOTHING MORE. "0", "false",
    "no" and "off" (any case) switch it off; blank, `1` and anything else leave
    it on.

    Implements: SR-070, LLR-1075
    """
    return (env.get("SLEEP_PUBLISH") or "1").strip().lower() not in ("0", "false", "no", "off")


def resolve_identity(env):
    """Return the ONE household identity the gauge is posted for, or refuse.

    Config: SLEEP_USER - the stable Google `sub`, the same value as
    NAGLIGHT_USER / AI_USAGE_USER / WEIGHT_USER. Blank is a REFUSAL, not a
    default: a sleep score on the wrong household member's board is a private
    health measurement shown to the wrong person on a wall in a shared room.

    Implements: SR-070, LLR-1074
    """
    value = (env.get("SLEEP_USER") or "").strip()
    if not value:
        raise SystemExit(
            "REFUSED: SLEEP_USER is unset. The feeder posts for ONE explicit "
            "identity (the stable Google `sub`, the same value as NAGLIGHT_USER) "
            "and will not guess whose sleep this is. Set it in stack/.env, or set "
            "SLEEP_ENABLED=false.")
    return value


def is_local_destination(host, bridge_addresses=None):
    """True for loopback or an address a LOCAL docker bridge really carries.

    A hostname is refused outright rather than resolved: resolution is a moving
    target and `localhost.example.com` is a real trick.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if bridge_addresses is None:
        bridge_addresses = local_bridge_networks()
    return any(addr in network for network in bridge_addresses)


def resolve_feed_url(env):
    """Return the /api/feed URL, refusing a destination off this box.

    The POST carries the household identity in `X-Forwarded-User` and (when set)
    the tracker bearer token. The tracker is bridge-only, so the only legitimate
    destinations are its loopback and its bridge address; the public https route
    would bounce through oauth2-proxy and OVERWRITE the identity header.

    Implements: SR-070, LLR-1074
    """
    raw = (env.get("SLEEP_FEED_URL") or "").strip()
    if not raw:
        raise SystemExit("REFUSED: SLEEP_FEED_URL is unset. There is no default: the "
                         "tracker is bridge-only, so the right value depends on this box.")
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception as exc:                      # pragma: no cover - defensive
        # The TYPE only: urlsplit echoes its input, which may carry userinfo.
        raise SystemExit("REFUSED: SLEEP_FEED_URL is unparseable (%s)" % type(exc).__name__)
    if parts.scheme not in ("http", "https"):
        raise SystemExit("REFUSED: SLEEP_FEED_URL scheme %r is not http/https" % parts.scheme)
    host = parts.hostname or ""
    if not host:
        raise SystemExit("REFUSED: SLEEP_FEED_URL has no host")
    if not is_local_destination(host):
        raise SystemExit(
            "REFUSED: SLEEP_FEED_URL host %r is neither loopback nor an address a "
            "local docker bridge is carrying. The POST carries the household "
            "identity; it must not leave this box." % host)
    return raw


def resolve_state_root(env):
    """The ONE directory this feeder may write under, whatever the config says.

    systemd exports $STATE_DIRECTORY for a unit carrying `StateDirectory=`. Only
    one directory is ever named, so the value is taken whole.
    """
    return (env.get("STATE_DIRECTORY") or "").strip() or DEFAULT_STATE_ROOT


def resolve_state_path(env):
    explicit = (env.get("SLEEP_STATE_FILE") or "").strip()
    return explicit or os.path.join(resolve_state_root(env), DEFAULT_STATE_NAME)


def resolve_timeout(env):
    try:
        value = int((env.get("SLEEP_TIMEOUT_SECONDS") or "").strip() or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if 1 <= value <= 300 else DEFAULT_TIMEOUT_SECONDS


def resolve_timezone(env, zoneinfo_module=None):
    """The hub's zone from the TIMEZONE knob, as a tzinfo; refuses rather than guesses.

    Night keys, the 14:00 grace and the 12:00 stamp are all LOCAL, so a feeder
    that guessed a zone would publish the wrong night around every evening.
    `Etc/UTC`/`UTC` work on any Python; any other name needs `zoneinfo`
    (Python 3.9+, the hub's) and the system tz database.

    Implements: SR-071, LLR-1084
    """
    name = (env.get("TIMEZONE") or "").strip()
    if not name:
        raise SystemExit("REFUSED: TIMEZONE is unset. Night keys are LOCAL dates, so the "
                         "feeder will not guess the hub's zone.")
    if name in ("UTC", "Etc/UTC", "Etc/Universal", "Zulu"):
        return dt.timezone.utc
    module = zoneinfo_module
    if module is None:
        try:
            import zoneinfo as module
        except ImportError:
            raise SystemExit("REFUSED: TIMEZONE=%s needs Python 3.9+ (zoneinfo)." % name)
    try:
        return module.ZoneInfo(name)
    except Exception:
        # The name only - it is our own configuration, not the remote's.
        raise SystemExit("REFUSED: TIMEZONE=%s is not a zone this box knows." % name)


def undouble_dollars(value):
    """Collapse the `$$` the materialiser writes for every `$` in .env values.

    Materialize-Deploy.ps1 doubles each `$` in the compose .env (compose
    interpolates `$VAR` in values), and systemd's EnvironmentFile= performs NO
    `$` interpretation, so a password with a `$` in it arrives here DOUBLED. The
    box-side loaders (firstboot.sh env_value) collapse it the same way; a single
    `$` is left alone.

    Implements: SR-072, LLR-1087
    """
    return value.replace("$$", "$")


def resolve_credentials(env):
    """(email, password) from MYAIR_EMAIL / MYAIR_PASSWORD, or refuse.

    The email reaches .env from the identity table (household.MyAirEmail), the
    password from the secret store (MyAirPassword). NEITHER IS EVER LOGGED,
    ECHOED OR PERSISTED: they are held in memory for one cycle and sent only to
    the two Okta calls that need them.

    Implements: SR-072, LLR-1087
    """
    email = (env.get("MYAIR_EMAIL") or "").strip()
    password = undouble_dollars(env.get("MYAIR_PASSWORD") or "")
    if not email or not password:
        raise SystemExit(
            "REFUSED: MYAIR_EMAIL or MYAIR_PASSWORD is blank. The email comes from the "
            "identity table (household.MyAirEmail) and the password from the secret "
            "store (MyAirPassword); with either missing there is nothing to sign in "
            "with. Set SLEEP_ENABLED=false to switch the feeder off.")
    return email, password


# ── SHELL: everything below touches the world ───────────────────────────────

def local_bridge_networks():
    """The networks the box's docker bridges are actually carrying (`ip -json addr`).

    An empty list is a legitimate answer and means only loopback is acceptable,
    which is the safe direction to fail.
    """
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


# ── The write guard: ONE file, inside StateDirectory= ───────────────────────

def writable_path_verdict(path, state_path, state_root, resolve=None):
    """Decide whether `path` may be written. Returns a refusal string, or None.

    realpath, not abspath: abspath is string arithmetic and does not resolve a
    symlink, so a `<state>.tmp` pre-planted as a link would pass a string
    allow-list and be truncated through. The resolved path must sit inside the
    service's OWN state directory (independent of what the .env names) AND be
    the state file or its `.tmp` sibling.

    Implements: SR-070, LLR-1073
    """
    resolve = resolve or os.path.realpath
    root = resolve(state_root)
    inside = os.path.join(root, "")
    real_state = resolve(state_path)
    resolved = resolve(path)
    if not (resolved == root or resolved.startswith(inside)):
        return ("%s resolves to %s, which is outside this service's own state "
                "directory %s." % (path, resolved, root))
    if resolved not in (real_state, real_state + ".tmp"):
        return ("%s resolves to %s, which is outside the one path this feeder may "
                "write (%s)." % (path, resolved, real_state))
    return None


def open_for_write(path, state_path, state_root):
    """The ONLY way this feeder opens a file for writing."""
    reason = writable_path_verdict(path, state_path, state_root)
    if reason:
        raise PermissionError("REFUSED: " + reason)
    return open_no_follow(path)


def open_no_follow(path):
    """Open `path` for writing, mode 0600, without ever following a symlink.

    The file is unlinked first (which removes a planted link, never its target),
    then created O_EXCL|O_NOFOLLOW so the kernel refuses if anything is there by
    the time we open. O_NOFOLLOW is 0 on Windows; the hub is Linux.
    """
    try:
        os.unlink(path)
    except OSError:
        pass
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")


def load_state(state_path, now, tz):
    """The state file, each part VALIDATED. A missing or unreadable file is empty.

    Five parts, each by its own validator (never an exemption): the nights, the
    published night keys, the fetch schedule, the consecutive-auth-failure
    counter, and the sticky auth marker. Whatever the
    loader does not vouch for is dropped here - and the next `save_state` would
    erase it - which is why each recognised part needs a validator at all.

    Implements: SR-071, SR-072, LLR-1085, LLR-1089
    """
    state = empty_state()
    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return state
    if not isinstance(data, dict):
        return state
    nights = data.get(NIGHTS_KEY)
    if isinstance(nights, dict):
        for key, entry in nights.items():
            checked = validate_night_entry(key, entry, now, tz)
            if checked is not None:
                state[NIGHTS_KEY][key] = checked
    state[PUBLISHED_KEY], dropped = validate_published(data.get(PUBLISHED_KEY), now, tz)
    if dropped:
        state["_notes"].append("ignored %d published night key(s) dated later than tomorrow "
                               "(corrupt state or a forward clock jump)" % dropped)
    state[SCHEDULE_KEY] = validate_schedule(data.get(SCHEDULE_KEY), now)
    state[AUTH_COUNTS_KEY] = validate_auth_counts(data.get(AUTH_COUNTS_KEY))
    state[AUTH_MARKER_KEY] = validate_auth_marker(data.get(AUTH_MARKER_KEY), now)
    return state


def save_state(state, state_path, state_root):
    """Write the state file through `open_for_write`, atomically, mode 0600.

    It holds night keys, scores and timestamps, a failure count and a marker -
    never a credential, a token, a header or a response body (asserted by test).
    The SCORE lives here, in a 0600 file under a 0700 directory, and nowhere
    else on the box.

    Implements: SR-070, LLR-1085
    """
    tmp = state_path + ".tmp"
    out = {NIGHTS_KEY: state[NIGHTS_KEY], PUBLISHED_KEY: state[PUBLISHED_KEY],
           AUTH_COUNTS_KEY: state[AUTH_COUNTS_KEY]}
    if state[SCHEDULE_KEY] is not None:
        out[SCHEDULE_KEY] = state[SCHEDULE_KEY]
    if state[AUTH_MARKER_KEY] is not None:
        out[AUTH_MARKER_KEY] = state[AUTH_MARKER_KEY]
    with open_for_write(tmp, state_path, state_root) as handle:
        json.dump(out, handle, indent=1, sort_keys=True)
    os.replace(tmp, state_path)


# ── HTTP: the feed side (loopback only) ─────────────────────────────────────

class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every 3xx instead of re-sending the request to `Location`.

    urllib FOLLOWS redirects and copies the original headers onto the new
    request. A feed endpoint that answered `302 Location: http://elsewhere/`
    would otherwise get the identity header and the body re-sent off this box.
    NEITHER THE TARGET NOR THE RESPONSE IS PUT IN THE MESSAGE.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EgressRefused(
            "refused an HTTP %s redirect: this request may not be re-sent to "
            "a destination the response chose" % code)


def _peer_checked_connection(base_class, allow_host):
    """A connection class that asks the socket where it really landed.

    The check runs inside `connect()`, after the handshake and BEFORE a byte of
    the request is written, so a connection to an address that is not on this
    box is dropped with the identity and the body still unsent.
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
                    "loopback nor an address a local docker bridge is carrying" % (peer,))

    return PeerChecked


class _LocalOnlyHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, allow_host):
        urllib.request.HTTPHandler.__init__(self)
        self._peer_checked = _peer_checked_connection(http.client.HTTPConnection, allow_host)

    def http_open(self, req):
        return self.do_open(self._peer_checked, req)


class _LocalOnlyHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allow_host):
        urllib.request.HTTPSHandler.__init__(self)
        self._peer_checked = _peer_checked_connection(http.client.HTTPSConnection, allow_host)

    def https_open(self, req):
        return self.do_open(self._peer_checked, req, context=self._context)


def feed_opener(bridge_addresses=None):
    """The opener the FEED POST uses: no proxy, no redirect, no off-box peer.

    `ProxyHandler({})` installs NO proxy rather than reading the environment (an
    exported `http_proxy` would route the identity header through a LAN proxy);
    `_RefuseRedirects` refuses to be told where to go; and the peer check asks
    the socket what it actually reached.

    Implements: SR-070, LLR-1074
    """
    def allow_host(host):
        return is_local_destination(host, bridge_addresses)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirects(),
        _LocalOnlyHTTPHandler(allow_host),
        _LocalOnlyHTTPSHandler(allow_host))


def post_gauge(body, url, env, timeout):
    """POST one gauge through the local-only opener. Returns (ok, detail).

    Never raises. THE REMOTE'S OWN WORDS ARE NEVER PUT IN `detail`: a responding
    server or an interposed proxy can reflect a header back, and `detail` goes to
    the journal.

    Implements: SR-070, LLR-1074
    """
    headers = {"Content-Type": "application/json"}
    token = env.get("SLEEP_FEED_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    headers["X-Forwarded-User"] = env["_identity"]
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with feed_opener().open(request, timeout=timeout) as response:
            return response.status == 200, "HTTP %s" % response.status
    except EgressRefused as exc:
        return False, str(exc)           # our own words, not the remote's
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s (body not logged)" % exc.code
    except Exception as exc:
        return False, type(exc).__name__


# ── HTTP: the myAir side (outbound, https only) ─────────────────────────────

# The NA region's public client identifiers, from the upstream integration's
# regions.py (MIT). They identify the myAir web app to Okta and AppSync; they
# are not the Owner's credentials and every user of the app sends the same ones.
Endpoints = namedtuple("Endpoints", (
    "okta_base", "auth_server_id", "client_id", "redirect_uri",
    "graphql_url", "api_key", "product", "https_only"))

NA_ENDPOINTS = Endpoints(
    okta_base="https://resmed-ext-1.okta.com",
    auth_server_id="aus4ccsxvnidQgLmA297",
    client_id="0oa4ccq1v413ypROi297",
    redirect_uri="https://myair.resmed.com",
    graphql_url="https://graphql.myair-prd.dht.live/graphql",
    api_key="da2-cenztfjrezhwphdqtwtbpqvzui",
    product="myAir",
    https_only=True)

# Upstream graphql.py sends these fixed web-client headers; AppSync gates on
# x-api-key, the bearer token and rmdcountry.
_APP_HEADERS = {
    "rmdhandsetid": "02c1c662-c289-41fd-a9ae-196ff15b5166",
    "rmdlanguage": "en",
    "rmdhandsetmodel": "Chrome",
    "rmdhandsetosversion": "127.0.6533.119",
    "rmdappversion": "1.0.0",
    "rmdhandsetplatform": "Web",
    "accept-language": "en-US,en;q=0.9",
}


class _NoFollow(urllib.request.HTTPRedirectHandler):
    """Returns the 3xx to the caller as an HTTPError instead of following it.

    The authorize call's ANSWER is a 302 whose `Location` fragment carries the
    code; we read that header and never request the URL. Every other call treats
    a 3xx as a failure, so a vendor redirect can never re-send the password or
    a bearer token anywhere.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def vendor_opener():
    """Outbound-to-the-internet by design: no inherited proxy, no followed redirect.

    A box-wide `https_proxy` would otherwise route the Okta password through a
    LAN proxy that terminates TLS; there is deliberately no knob to opt back in.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoFollow())


def _require_https(url, endpoints):
    if url.lower().startswith("https://"):
        return
    if endpoints.https_only:
        raise SourceFailure("refused - a myAir URL carrying credentials must be https")


def _request(opener, endpoints, step, url, timeout, method="GET", headers=None,
             body=None, cookies=None):
    """One request. Returns (status, headers, body_bytes_or_None).

    HTTP error statuses are RETURNED, not raised, so the caller classifies them
    (a 401 is an AuthFailure; a 500 is not). Only a 2xx carries a body. A
    transport failure becomes a SourceFailure naming the step and the exception
    TYPE - never the URL's query (it holds the session token) and never a body.
    """
    _require_https(url, endpoints)
    merged = dict(headers or {})
    if cookies:
        merged["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in sorted(cookies.items()))
    request = urllib.request.Request(url, data=body, headers=merged, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise SourceFailure("%s: the response is implausibly large" % step)
            return response.status, response.headers, raw
    except urllib.error.HTTPError as exc:
        status, headers_out = exc.code, exc.headers
        try:
            exc.close()
        except Exception:                                  # pragma: no cover
            pass
        return status, headers_out, None
    except (SourceFailure, EgressRefused):
        raise
    except Exception as exc:
        raise SourceFailure("%s: %s" % (step, type(exc).__name__))


def _json_body(raw, step):
    if raw is None:
        raise SourceFailure("%s: no body" % step)
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SourceFailure("%s: the body is not JSON" % step)
    if not isinstance(value, dict):
        raise SourceFailure("%s: the body is not an object" % step)
    return value


def _absorb_cookies(headers, jar):
    """Keep the two cookies Okta's flow uses (DT, sid) from Set-Cookie headers."""
    for header in headers.get_all("Set-Cookie") or []:
        try:
            parsed = http.cookies.SimpleCookie(header)
        except http.cookies.CookieError:
            continue
        for name, morsel in parsed.items():
            lowered = name.lower()
            if lowered in ("dt", "sid") and morsel.value:
                jar["DT" if lowered == "dt" else "sid"] = morsel.value


def pkce_pair():
    """(verifier, challenge): an S256 PKCE pair, as the upstream flow builds it."""
    verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode("ascii"))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").replace("=", "")
    return verifier, challenge


def country_from_id_token(id_token):
    """The `myAirCountryId` claim of the (unverified) id token, for AppSync's header.

    The signature is not verified because nothing here trusts the claim for
    anything but a routing header on a call that carries our own access token.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (AttributeError, IndexError, ValueError, UnicodeError):
        raise SourceFailure("token: the id token could not be decoded")
    country = claims.get("myAirCountryId") if isinstance(claims, dict) else None
    if not isinstance(country, str) or not _COUNTRY_SHAPE.match(country):
        raise SourceFailure("token: the id token carries no country")
    return country


def sign_in(email, password, endpoints, timeout, opener=None):
    """Okta primary auth -> authorization code (PKCE) -> tokens. Returns (access, id).

    THE FLOW S0b OBSERVED (NA, 2026-10-11): GET authorize (a 400 that hands out
    the remembered-device cookie), POST authn (200, status SUCCESS, no email
    MFA), GET authorize with the session token (302, the code in the Location
    FRAGMENT), POST token (200). There is no refresh token (`offline_access` is
    not requested), so this runs from scratch every cycle; tokens last 3600 s.

    CLASSIFICATION (plan 3.6 G2) - what is an AuthFailure:
      * an Okta HTTP 401 at authn or token;
      * an authn `status` that is not SUCCESS: MFA_REQUIRED (MfaRequired), and
        LOCKED_OUT, PASSWORD_EXPIRED and the rest by their allow-listed word.
    Everything else (5xx, 429, a timeout, a missing field, a refused redirect)
    is a plain SourceFailure. No Okta error text is ever reproduced.

    Implements: SR-072, LLR-1086, LLR-1088
    """
    opener = opener or vendor_opener()
    jar = {}
    base = endpoints.okta_base
    authorize = "%s/oauth2/%s/v1/authorize" % (base, endpoints.auth_server_id)
    json_headers = {"Content-Type": "application/json", "Accept": "application/json"}

    # 1. Prime the remembered-device cookie. Okta answers 400 here; that is the
    #    expected, harmless answer. Only the Set-Cookie matters.
    _status, headers, _raw = _request(opener, endpoints, "okta-prime", authorize, timeout,
                                      headers=json_headers)
    _absorb_cookies(headers, jar)

    # 2. Primary authentication. The password goes in this one body and nowhere else.
    credentials = json.dumps({"username": email, "password": password}).encode("utf-8")
    status, headers, raw = _request(
        opener, endpoints, "okta-authn", "%s/api/v1/authn" % base, timeout, method="POST",
        headers=json_headers, body=credentials, cookies=jar)
    if status == 401:
        raise AuthFailure("okta-authn: HTTP 401", status=401)
    if status != 200:
        raise SourceFailure("okta-authn: HTTP %s" % status)
    authn = _json_body(raw, "okta-authn")
    outcome = authn.get("status")
    if not isinstance(outcome, str):
        raise SourceFailure("okta-authn: no status in the response")
    if outcome == "MFA_REQUIRED":
        raise MfaRequired("okta-authn: MFA_REQUIRED", code="MFA_REQUIRED")
    if outcome != "SUCCESS":
        word = outcome if outcome in _OKTA_STATUSES else "UNRECOGNISED"
        raise AuthFailure("okta-authn: status %s" % word, code=word)
    session_token = authn.get("sessionToken")
    if not isinstance(session_token, str) or not session_token:
        raise SourceFailure("okta-authn: SUCCESS without a session token")
    _absorb_cookies(headers, jar)

    # 3. Exchange the session token for an authorization code (PKCE).
    verifier, challenge = pkce_pair()
    query = urllib.parse.urlencode({
        "client_id": endpoints.client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "none",
        "redirect_uri": endpoints.redirect_uri,
        "response_mode": "fragment",
        "response_type": "code",
        "sessionToken": session_token,
        "scope": "openid profile email",
        "state": secrets.token_urlsafe(12),      # no browser, nothing to forge
    })
    status, headers, _raw = _request(opener, endpoints, "okta-authorize",
                                     authorize + "?" + query, timeout,
                                     headers=json_headers, cookies=jar)
    if status != 302:
        raise SourceFailure("okta-authorize: HTTP %s (expected 302)" % status)
    location = headers.get("Location") or ""
    fragment = urllib.parse.urldefrag(location).fragment
    codes = urllib.parse.parse_qs(fragment).get("code", [])
    if not codes:
        raise SourceFailure("okta-authorize: no authorization code in the redirect")
    _absorb_cookies(headers, jar)

    # 4. Exchange the code for tokens.
    form = urllib.parse.urlencode({
        "client_id": endpoints.client_id,
        "redirect_uri": endpoints.redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
        "code": codes[0],
    }).encode("ascii")
    status, _headers, raw = _request(
        opener, endpoints, "okta-token", "%s/oauth2/%s/v1/token" % (base, endpoints.auth_server_id),
        timeout, method="POST",
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        body=form, cookies=jar)
    if status == 401:
        raise AuthFailure("okta-token: HTTP 401", status=401)
    if status != 200:
        raise SourceFailure("okta-token: HTTP %s" % status)
    tokens = _json_body(raw, "okta-token")
    access, ident = tokens.get("access_token"), tokens.get("id_token")
    if not isinstance(access, str) or not access or not isinstance(ident, str) or not ident:
        raise SourceFailure("okta-token: no access or id token in the response")
    return access, ident


def graphql_query_text(today):
    """The nightly query: the last 30 days, ONLY the two fields the contract uses.

    The upstream query also asks for the sub-scores, usage and leak figures.
    This feeder does not use them, so it does not ask: fewer health values
    cross the wire and sit in memory. (Field names and meanings are in the
    README, from the S0b probe.)
    """
    first = (today - dt.timedelta(days=MYAIR_WINDOW_DAYS)).isoformat()
    return ("query GetPatientSleepRecords { getPatientWrapper { "
            'sleepRecords(startMonth: "%s", endMonth: "%s") '
            "{ items { startDate sleepScore __typename } __typename } "
            "__typename } }" % (first, today.isoformat()))


def fetch_records(access, ident, endpoints, today, timeout, opener=None):
    """POST the nightly query. Returns ([Record], unreadable_count).

    A GraphQL HTTP 401, or a 200 carrying `errors[0].errorInfo.errorType ==
    "unauthorized"`, after a sign-in that just succeeded is an AuthFailure (the
    account's access was refused). Any other error is a plain SourceFailure
    with a fixed message: the server's words are not reproduced.

    Implements: SR-072, LLR-1086, LLR-1088
    """
    opener = opener or vendor_opener()
    headers = dict(_APP_HEADERS)
    headers.update({
        "Content-Type": "application/json",
        "x-api-key": endpoints.api_key,
        "Authorization": "Bearer " + access,
        "rmdproduct": endpoints.product,
        "rmdcountry": country_from_id_token(ident),
    })
    body = json.dumps({"operationName": "GetPatientSleepRecords", "variables": {},
                       "query": graphql_query_text(today)}).encode("utf-8")
    status, _headers, raw = _request(opener, endpoints, "graphql", endpoints.graphql_url,
                                     timeout, method="POST", headers=headers, body=body)
    if status == 401:
        raise AuthFailure("graphql: HTTP 401", status=401)
    if status != 200:
        raise SourceFailure("graphql: HTTP %s" % status)
    payload = _json_body(raw, "graphql")
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else None
        info = first.get("errorInfo") if isinstance(first, dict) else None
        kind = info.get("errorType") if isinstance(info, dict) else None
        if kind == "unauthorized":
            raise AuthFailure("graphql: unauthorized", code="unauthorized")
        raise SourceFailure("graphql: the query was refused")
    return parse_records(payload)


def read_myair(env, now, tz, endpoints=NA_ENDPOINTS):
    """The one source: sign in fresh, fetch, return ([Record], unreadable_count).

    Plain function over `env`/`now` so `run_cycle` can swap it for a fake.
    """
    email, password = resolve_credentials(env)
    timeout = resolve_timeout(env)
    access, ident = sign_in(email, password, endpoints, timeout)
    return fetch_records(access, ident, endpoints, expected_night(now, tz), timeout)


# ── The cycle ───────────────────────────────────────────────────────────────

def run_cycle(env, now=None, tz=None, reader=None, poster=None, clock=None,
              force_fetch=False, retry_now=False, sync_check=None):
    """One feeder cycle: maybe read, fold into the nights, decide, post one gauge.

    Contract:
      Inputs:  env: the process environment (plus `_identity`, `_feed_url`);
               `reader(env, now, tz)` and `poster(body, url, env, timeout)` are
               injectable so the tests can exercise every failure class;
               `clock()` returns the current epoch seconds and is read AGAIN
               after the fetch (default: `time.time`, or the injected `now`);
               `force_fetch` ignores the fetch windows (never the back-off);
               `retry_now` clears a confirmed sign-in failure (and any published
               key dated later than today) first and then forces a fetch (the
               operator re-signed; see README);
               `sync_check()` returns (True|False|None, method) for "is the host
               clock synchronised" (default: `clock_synchronized`).
      Outputs: (posted, notes, failures):
                 posted   - [(night_key_or_None, ok)]: exactly one entry, or none
                            when the clock went backwards and nothing was posted;
                 notes    - status lines for stdout (sources, night key, "published
                            night <key>"): NEVER a score or any health value;
                 failures - lines for stderr; non-empty makes the unit exit 1.

    THE ORDER IS THE CONTRACT:
      00. refuse to do anything unless the host clock is synchronised (fetch
         nothing, post nothing new, log status);
      0. refuse to do anything if the local date is earlier than the newest
         published night (the clock went backwards);
      1. decide whether myAir may be asked THIS tick (`fetch_due`): the timer
         ticks every 30 min, the network does not;
      2. read myAir if so (a failure is a source failure, not a dead cycle);
      3. RE-READ THE CLOCK. A cycle that began at 13:59 and finished at 14:01
         applies the 14:00 rule at 14:01, and a stamp is never older than the
         moment it was decided;
      4. count authentication failures in the state file and set / refresh /
         clear the sticky marker - only if myAir was actually asked (a skipped
         tick is not an outcome and resets nothing);
      5. on a SUCCESSFUL read only, fold the records into the stored nights;
      6. decide which stored night, if any, is shown (`decide_night`), or none
         while the login is confirmed dead;
      7. post exactly one gauge; if it carried a score and was accepted, record
         the night as PUBLISHED; save the state EVEN IF THE POST FAILED so the
         frozen `observed_at` is never lost.

    Implements: SR-070, SR-071, SR-072, LLR-1076, LLR-1082, LLR-1083, LLR-1084,
                LLR-1086, LLR-1089, LLR-1090
    """
    if clock is None:
        clock = time.time if now is None else (lambda: now)
    start = int(now if now is not None else clock())
    tz = tz or env["_tz"]
    reader = reader or read_myair
    state_path = resolve_state_path(env)
    state_root = resolve_state_root(env)
    feed_url = env["_feed_url"]
    notes, failures = [], []

    synced, method = (sync_check or clock_synchronized)()
    if synced is not True:
        # NOT SYNCHRONISED: skip myAir and every TIMESTAMPED publication, but still
        # POST THE UNAVAILABLE BODY. It carries no `observed_at`, so it needs no
        # trusted time - and posting it is what makes the 14:00 cutoff hold after
        # a reboot with NTP down: yesterday's green score must not sit on the wall
        # past 14:00 just because the clock cannot be trusted. Every unsynced tick,
        # any hour. (A hub with no synchronisation signal at all shows grey, not
        # nothing.) State is neither read nor written.
        try:
            ok, detail = (poster or post_gauge)(build_gauge(None, None), feed_url, env,
                                                resolve_timeout(env))
        except Exception as exc:                   # a poster bug is a failed post
            ok, detail = False, type(exc).__name__
        problems = ["the host clock is not known to be synchronised (%s) - publishing the "
                    "unavailable (grey) gauge and fetching nothing until it is; check "
                    "systemd-timesyncd (systemctl status systemd-timesyncd) and "
                    "`timedatectl show -p NTPSynchronized`" % method]
        if not ok:
            problems.append("post %s: %s" % (GAUGE_ID, detail))
        return [(None, ok)], notes, problems

    state = load_state(state_path, start, tz)
    notes.extend(state.pop("_notes"))
    if retry_now:
        # An operator's way out of a wedge: a published key dated later than today
        # (a forward clock jump that was later corrected) would make
        # `clock_went_backwards` refuse every cycle until the calendar caught up.
        today_date = expected_night(start, tz)
        stale = [k for k in state[PUBLISHED_KEY] if parse_night_date(k) > today_date]
        for key in stale:
            del state[PUBLISHED_KEY][key]
        if stale:
            notes.append("retry-now: cleared %d published night key(s) dated later than today"
                         % len(stale))
    backwards = ("the clock appears to have gone backwards (the local date is earlier than "
                 "the newest published night) - nothing selected, nothing posted")
    if clock_went_backwards(state, start, tz):
        return [], notes, [backwards]

    if retry_now and state[AUTH_MARKER_KEY] is not None:
        notes.append("retry-now: the confirmed sign-in failure marker was cleared")
    if retry_now:
        state[AUTH_MARKER_KEY] = None
        state[AUTH_COUNTS_KEY] = {}
    due, why, slots = (True, "forced", []) if (force_fetch or retry_now) and \
        state[AUTH_MARKER_KEY] is None else fetch_due(state, start, tz)
    if not due:
        notes.append("not asked: %s" % why)

    records, auth_error, unreadable = None, None, 0
    if due:
        schedule = state[SCHEDULE_KEY]
        day = expected_night(start, tz).isoformat()
        prior = schedule if schedule is not None and schedule["day"] == day else None
        # `attempt_at` is recorded for EVERY attempt; the slots are marked done
        # only after a read that did not fail (below), so a transient failure at
        # 20:00 does not consume the evening catch-up.
        state[SCHEDULE_KEY] = {"day": day, "attempt_at": start,
                               "done": list(prior["done"]) if prior else [],
                               "fails": dict(prior["fails"]) if prior else {}}
        try:
            records, unreadable = reader(env, start, tz)
        except MfaRequired as exc:
            auth_error = exc
            failures.append("%s: %s" % (SOURCE, MFA_MESSAGE))
        except AuthFailure as exc:
            auth_error = exc
            failures.append("%s: %s" % (SOURCE, exc))
        except SourceFailure as exc:
            failures.append("%s: %s" % (SOURCE, exc))
        except Exception as exc:                     # a reader bug is a source failure
            failures.append("%s: unexpected %s" % (SOURCE, type(exc).__name__))

    # ── The clock, read AGAIN: everything from here on uses the fresh time ───
    now = int(clock())
    if clock_went_backwards(state, now, tz):
        return [], notes, failures + [backwards]
    expected = expected_night(now, tz).isoformat()

    if due and slots:
        schedule = state[SCHEDULE_KEY]
        if records is not None:
            schedule["done"] = sorted(set(schedule["done"]) | set(slots))
            schedule["fails"] = {}
        elif auth_error is None:
            schedule["fails"][slots[-1]] = schedule["fails"].get(slots[-1], 0) + 1

    # ── Authentication bookkeeping (plan 3.6 G2), only if myAir was asked ────
    if due:
        previous = state[AUTH_COUNTS_KEY].get(SOURCE, 0)
        if auth_error is not None:
            count = min(previous + 1, AUTH_CONFIRM_CYCLES)
            state[AUTH_COUNTS_KEY] = {SOURCE: count}
            if count >= AUTH_CONFIRM_CYCLES or state[AUTH_MARKER_KEY] is not None:
                # Created on confirmation, REFRESHED by every later refused
                # attempt: the back-off runs from the last refusal, not the first.
                marker = {"auth_failed": True, "at": now}
                if isinstance(auth_error.code, str) and _CODE_SHAPE.match(auth_error.code):
                    marker["code"] = auth_error.code
                state[AUTH_MARKER_KEY] = marker
                failures.append("%s: authentication failure %d of %d - CONFIRMED, the gauge is "
                                "posted unavailable and myAir is not asked again for %d h "
                                "(or until --retry-now)"
                                % (SOURCE, count, AUTH_CONFIRM_CYCLES,
                                   backoff_seconds(marker) // 3600))
            else:
                failures.append("%s: authentication failure %d of %d - last score kept until "
                                "confirmed" % (SOURCE, count, AUTH_CONFIRM_CYCLES))
        else:
            # A success OR a failure of another kind: the count is CONSECUTIVE
            # authentication failures, so either resets it. Only a success clears
            # the marker: a network blip after a confirmed login death must not
            # bring the old colour back (the sticky rule).
            state[AUTH_COUNTS_KEY] = {}
            if records is not None:
                state[AUTH_MARKER_KEY] = None

    # ── Fold, decide, post ───────────────────────────────────────────────────
    if records is not None:
        notes.append("source %s answered" % SOURCE)
        if unreadable:
            notes.append("%d record(s) had an unreadable startDate and were skipped" % unreadable)
        changed = upsert_nights(state[NIGHTS_KEY], records, now, tz)
        if changed:
            notes.append("night key(s) updated: %s" % ", ".join(sorted(changed)))
    pruned_future = prune_nights(state, now, tz)
    if pruned_future:
        notes.append("pruned %d night key(s) dated later than tomorrow" % pruned_future)

    dead = state[AUTH_MARKER_KEY] is not None
    night = None if dead else decide_night(state[NIGHTS_KEY], state[PUBLISHED_KEY], now, tz)
    if night is None:
        body = build_gauge(None, None)
    else:
        entry = state[NIGHTS_KEY][night]
        body = build_gauge(entry["score"], rfc3339_local(entry["observed_at"], tz))

    try:
        ok, detail = (poster or post_gauge)(body, feed_url, env, resolve_timeout(env))
    except Exception as exc:                       # a poster bug is a failed post
        ok, detail = False, type(exc).__name__
    if not ok:
        failures.append("post %s: %s" % (GAUGE_ID, detail))
    if night is not None:
        if ok:
            state[PUBLISHED_KEY].setdefault(night, now)
        notes.append("published night %s" % night if ok else "night %s not published" % night)
    elif dead:
        notes.append("expected night %s: login confirmed dead - posted unavailable" % expected)
    else:
        notes.append("expected night %s: no score yet - posted unavailable" % expected)

    try:
        save_state(state, state_path, state_root)
    except OSError as exc:
        # Including PermissionError from the write guard. The gauge is already
        # posted; losing the history is a named failure, not a crash.
        failures.append("state %s: %s" % (state_path, type(exc).__name__))
    return [(night, ok)], notes, failures


def main(argv=None):
    """Entry point: gate, resolve, one cycle, report. It does not compute.

    `--check` validates the configuration and exits. `--retry-now` is for the
    operator who has just re-signed the myAir account: it clears a confirmed
    sign-in failure and asks myAir at once, outside the fetch windows.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    if not resolve_enabled(env):
        print("sleep: SLEEP_ENABLED is not true - nothing to do.")
        return 0
    env["_identity"] = resolve_identity(env)
    env["_feed_url"] = resolve_feed_url(env)
    env["_tz"] = resolve_timezone(env)
    resolve_credentials(env)                 # refuses on blank; discards the values
    if "--check" in argv:
        print("sleep: configuration accepted (identity set, destination local, "
              "zone known, sign-in configured).")
        return 0
    if not resolve_publish(env):
        print("sleep: SLEEP_PUBLISH is off - nothing read, nothing posted.")
        return 0
    if "--retry-now" in argv:
        reason = retry_now_refusal(resolve_state_path(env))
        if reason:
            print("sleep: REFUSED: %s" % reason, file=sys.stderr)
            return 2
    _posted, notes, failures = run_cycle(env, retry_now="--retry-now" in argv)
    for line in notes:
        print("sleep: %s" % line)
    for line in failures:
        print("sleep: %s" % line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
