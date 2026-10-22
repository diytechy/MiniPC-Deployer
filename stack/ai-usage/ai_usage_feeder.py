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

Two readers were added 2026-10-11 (plan R2, docs/GAUGE_RINGS_AND_SLEEP_PLAN):

  claude2   the same endpoint as `claude`, for a SECOND account whose CLI
            config lives in its own CLAUDE_CONFIG_DIR. Ids `ai-usage-claude2-*`.
  gemini    POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota,
            the call behind Gemini CLI's own `/stats` (gemini-cli
            packages/core/src/code_assist/server.ts and config/config.ts
            `refreshUserQuota`, HEAD 9b6e026), after one `loadCodeAssist` call
            for the project id. The body shape comes from that SOURCE, not from
            an observed response: no live call was possible when it was written,
            so the parser is pinned to `types.ts` and README.md lists what is
            still unproven. Unlike the three observed sources above it fails
            closed on anything it does not recognise.

THE FOUR ACCEPTANCE PROPERTIES, each with the symbol that enforces it:
  1. never writes a vendor credential file  -> `open_for_write`
  2. a failing source posts "unavailable", never a green gauge
                                            -> `build_post` (+ `is_fresh`)
  3. scoped to ONE explicit identity, refusing to guess
                                            -> `resolve_identity`
  4. off by default                         -> `resolve_enabled` / setup script

Implements: SR-021, LLR-005
"""

import calendar
import contextvars
import email.utils
import http.client
import json
import math
import os
import re
import signal
import stat
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
#     is absent.
GAUGE_UNIT = "%"
GAUGE_MIN = 0.0
GAUGE_MAX = 100.0

# THE WIRE CARRIES PLAN REMAINING, NOT PLAN CONSUMED (Owner ruling 2026-09-12).
#
# This pairing is load-bearing and the previous one was silently broken. The
# feeder used to post `target: 0` WITH `direction: "up"`, and NagLight runs an
# "up" pace from `min` to `target` -- which is 0 to 0. The served `pace` was
# therefore ZERO at every point of every window, for every gauge this feeder has
# ever posted. The dashed line sat on the floor of each bar, and NagLight's
# deviation collapsed to `value / 50`, so the bar's colour became a pure
# function of percent consumed: full red at 50% used no matter where the window
# was. OpenCode weekly showed red at 78% used on day six of seven -- a healthy
# burn -- exactly as it would at 51% on day one, which is an emergency.
#
# Counting DOWN from a full bar fixes it without a contract change: `pace`
# becomes `100 * (1 - elapsed)`, the plan you ought to have left right now, and
# the bar drains as the window burns.
GAUGE_TARGET = 0.0          # nothing left at the reset is where the pace line ends.
GAUGE_DIRECTION = "down"    # ... and it falls to there from a full bar at the start.

# THE "NEVER MEASURED ANYTHING" SENTINEL, IN CONSUMED TERMS, AND IT MUST BE THE
# PESSIMISTIC END OF THE BAR.
#
# When a source fails and no previous reading exists, the feeder still posts a
# gauge so the panel can say "unavailable" rather than silently dropping a
# limit. That body carries NO `observed_at`, which NagLight treats as stale on
# arrival, so the number is never rendered as a live reading -- but it is the
# one number in this file that was not measured, and it must fail in the safe
# direction if staleness ever stops protecting it.
#
# Under the old count-up shape that number was 0, meaning "nothing consumed",
# which read as an empty bar. Inverting to remaining turned the SAME literal
# into 100 % remaining -- a full, reassuring bar for a source that has never
# answered. Naming it in consumed terms keeps the intent where the flip happens:
# a fully consumed plan inverts to an empty bar, which is what an unmeasured
# gauge should look like if it is ever seen at all.
UNAVAILABLE_CONSUMED = GAUGE_MAX
RUNE_LIMIT = 120            # id/label/icon/unit are capped in RUNES, not bytes.

# NagLight's staleness horizons, by window kind. Kept here because the feeder's
# cadence has to be chosen against them: a WEEKLY gauge goes stale after 24h, so
# a weekly feeder that runs daily is permanently on the edge of "unavailable".
# The timer (one minute) and every per-source interval (SOURCE_INTERVAL_MAX_SECONDS)
# sit far below the tightest horizon for that reason.
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


class PendingState(SourceFailure):
    """An EXPECTED, quiet state - not a failure (Owner 2026-10-11).

    Two of them exist: the source's CLI is "not installed yet" (provisioning will
    install it) and "login owed" (the vendor sign-in has not happened, so there is
    no credential). Both post UNAVAILABLE exactly as a failed source does, because
    it IS a SourceFailure to every reader and to `build_post`; but `run_cycle`
    records it as a NOTE, not a failure, so it logs one INFO line and does not
    make the unit exit 1. A genuine error (a credential that exists and is
    unreadable, a non-200, a crash) is still a failure.
    """


def path_absent(path):
    """True ONLY when nothing at all is at `path`: lstat says ENOENT or ENOTDIR.

    Quiet means GENUINELY ABSENT. A path that is there but cannot be used - a
    dangling symlink, a directory we may not enter (EACCES), an I/O error - is NOT
    absent, so `os.path.exists` (which answers False for all of those) is the wrong
    test and this is the only one used to decide that something is "owed"."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return False
    else:
        return False
    # lstat only vouches for the last component: with a DANGLING ANCESTOR symlink
    # (~/.claude -> a missing target) every descendant also says ENOENT, yet that is a
    # broken install, not a login that is owed. Walk up to the first ancestor that
    # exists; if it is a link whose target is gone, the path is not "absent".
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        try:
            st = os.lstat(parent)
        except (FileNotFoundError, NotADirectoryError):
            parent = os.path.dirname(parent)
            continue
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            try:
                os.stat(parent)
            except OSError:
                return False          # a dangling (or unreadable) link above the path
        break
    return True


def binary_absent(binary, env):
    """True only when nothing named `binary` exists anywhere it would be found.

    A launch FileNotFoundError does not prove that: a missing interpreter or
    loader raises it for a binary that is very much installed. Existence is judged
    with lstat, so a dangling link or a non-executable file counts as PRESENT (and
    its launch failure stays loud). This is the SAME rule as the keeper units' gate
    (ConditionPathExists=| plus ConditionPathIsSymbolicLink=|: skipped only when
    nothing at all is at the path), so both sides agree on what "absent" means."""
    if os.sep in binary:
        return path_absent(os.path.expanduser(binary))
    search = env.get("PATH") or os.environ.get("PATH") or os.defpath
    return all(path_absent(os.path.join(d or ".", binary)) for d in search.split(os.pathsep))


def provisioning_hint(env, key):
    """' (provisioning: <reason>)' when the provisioner has recorded why `key` is
    pending, else ''. Best effort: a missing or odd state file is just no hint."""
    state_dir = env.get("AI_CLIS_STATE_DIR") or "/var/lib/homehub-ai-clis"
    try:
        with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as handle:
            reason = json.load(handle)["clis"][key]["pending"]["reason"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return " (provisioning: %s)" % reason if isinstance(reason, str) and reason else ""


class AuthFailure(SourceFailure):
    """A SourceFailure that is KNOWN to be an authentication failure.

    Plan 3.6 (G2): a dead login must go grey, not keep showing its last number
    in colour until NagLight's staleness horizon passes. To do that the cycle
    has to tell "the login is dead" from "the network blinked", and
    `http_json` used to flatten a 401 and a 500 into the same string. This is
    the typed alternative. It IS a SourceFailure, so every existing handler
    still works, but the per-source loop in `run_cycle` catches it FIRST.

    NO READER MAY CATCH ONE AND RE-RAISE IT AS A PLAIN SourceFailure -- that
    would silently turn a dead login back into "a network blip" and the gauge
    would never grey. The conversion only ever goes the other way (see
    `read_claude`, which promotes "carries no usable key" because only the
    Claude reader may read that text as authentication).

    `status` is the HTTP status and `code` the vendor's own error word
    (`token_expired`) -- whichever the raiser knew. Neither is a secret and
    neither is the remote's free text.

    Implements: SR-068, LLR-1053
    """

    def __init__(self, message, status=None, code=None):
        SourceFailure.__init__(self, message)
        self.status = status
        self.code = code


class Throttled(SourceFailure):
    """A SourceFailure in which the vendor asked us to come back LATER: an HTTP
    429 or any 5xx (W11, 2026-10-22).

    Like AuthFailure it IS a SourceFailure, so the gauge path is unchanged (the
    last reading is re-posted at its true `observed_at`); what differs is the
    SCHEDULE. `run_cycle` catches it before the plain handler and backs the
    source off: `retry_after` (seconds, already parsed from either form of the
    header by `parse_retry_after`) is honoured when the vendor sent one, and an
    exponential delay to BACKOFF_CEILING_SECONDS is used when it did not.

    `status` is the HTTP status. Neither field is the remote's free text.

    Implements: SR-095, LLR-1192
    """

    def __init__(self, message, status=None, retry_after=None):
        SourceFailure.__init__(self, message)
        self.status = status
        self.retry_after = retry_after


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

    `optional` marks a gauge the vendor may legitimately never mention. The
    five original gauges are NOT optional: each is a plan limit the household
    knows it has, so one going missing is news and must show as "unavailable"
    rather than vanishing. A per-model scoped limit is different -- whether it
    appears at all depends on the plan -- and a gauge that reads "unavailable"
    every cycle forever is worse than no gauge, because it teaches people to
    ignore the word. So an optional spec is posted only once there is something
    to say about it: a reading this cycle, or a stored one from a cycle that
    had it. Once seen it keeps being posted, so a limit that DISAPPEARS still
    goes stale in plain sight instead of silently leaving the wall.
    """

    __slots__ = ("key", "label", "icon", "source", "optional")

    def __init__(self, key, label, icon, source, optional=False):
        self.key = key
        self.label = label
        self.icon = icon
        self.source = source
        self.optional = optional


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

# PER-MODEL USAGE IS NOT A TOP-LEVEL KEY, AND THE ONES THAT LOOK LIKE IT ARE A
# TRAP. Measured against the live endpoint 2026-09-12: there is no
# `seven_day_fable`; `seven_day_opus` and `seven_day_sonnet` are both null; and
# `seven_day_breakdown` is a split by SURFACE (Claude Code / Chats / Cowork /
# Other) whose rows are shares of usage summing to 100, not shares of a quota.
#
# The body also carries `nimbus_quill`, `cinder_cove`, `copper_kite`,
# `harbor_lantern`, `amber_ladder`, `juniper_tide`, `tangelo`,
# `iguana_necktie`, `omelette_promotional`, `seven_day_cowork` and
# `seven_day_omelette` -- unreleased-model placeholders, mostly null, whose
# NAMES ARE NOT CONTRACTUAL. Keying on one would break silently the day it is
# renamed or ships.
#
# The only per-model signal on the wire is `limits[]`, where a scoped limit
# names its model in `scope.model.display_name`:
#
#   {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
#    "resets_at": "...", "scope": {"model": {"id": null,
#                                            "display_name": "Fable"}}}
#
# `scope.model.id` is null, so the display name is the only handle there is.
# Each entry is (display name, gauge key, label, window length in seconds).
CLAUDE_SCOPED_MODELS = (
    ("Fable", "ai-usage-claude-weekly-fable", "Claude F5", 7 * 24 * 3600),
)

# The kind a per-model weekly limit carries. Anything else in `limits[]` is a
# window this feeder already reads from a top-level bucket, or one it does not
# know; either way it is not a scoped model reading.
CLAUDE_SCOPED_KIND = "weekly_scoped"

# THE SECOND ACCOUNT (plan 3.3, "Claude 2"). Same endpoint, same payload, its own
# gauge ids -- `ai-usage-claude2-*`, because the panel groups rings by the
# provider id it reads out of the id (`/^ai-usage-([a-z0-9]+)(?:-|$)/`), and two
# accounts sharing `claude` would merge into one ring. The labels carry the "2"
# for the same reason a person reading the tracker needs it.
CLAUDE2_BUCKETS = (
    ("five_hour", "ai-usage-claude2-session", "Claude 2 session", 5 * 3600),
    ("seven_day", "ai-usage-claude2-weekly", "Claude 2 weekly", 7 * 24 * 3600),
)
CLAUDE2_SCOPED_MODELS = (
    ("Fable", "ai-usage-claude2-weekly-fable", "Claude 2 F5", 7 * 24 * 3600),
)


def parse_claude(payload, now, buckets=CLAUDE_BUCKETS,
                 scoped_models=CLAUDE_SCOPED_MODELS, name="claude"):
    """Turn one OAuth usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed;
               now: this cycle's clock, against which each window is checked;
               buckets / scoped_models / name: which account's gauge table to
               read into. The defaults are account 1, so every pre-existing
               caller is unchanged; `read_claude2` passes the CLAUDE2_* tables.
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
        raise SourceFailure("%s: body is not an object" % name)
    out = {}
    problems = []
    for field, key, _label, window_seconds in buckets:
        bucket = payload.get(field)
        if not isinstance(bucket, dict):
            problems.append("%s absent or null" % field)
            continue
        try:
            where = "%s %s" % (name, field)
            percent = check_percent(bucket.get("utilization"), where)
            resets_at = bucket.get("resets_at")
            window_end, window_seconds = check_window(
                None if resets_at is None
                else parse_iso8601_utc(resets_at, where),
                window_seconds, now, where)
        except SourceFailure as exc:
            problems.append(str(exc))
            continue
        out[key] = Reading(percent, window_end, window_seconds)
    out.update(parse_claude_scoped(payload, now, problems, scoped_models, name))
    if not out:
        raise SourceFailure("%s: no usable bucket (%s)" % (name, "; ".join(problems)))
    return out


def parse_claude_scoped(payload, now, problems, scoped_models=CLAUDE_SCOPED_MODELS,
                        source_name="claude"):
    """Per-model weekly readings out of `limits[]`, keyed on the model's name.

    Contract:
      Inputs:  payload: the decoded JSON body; now: this cycle's clock;
               problems: the caller's list, appended to for anything skipped so
               one failure message names every gap.
      Outputs: dict of gauge key -> Reading, one per CLAUDE_SCOPED_MODELS entry
               that is present AND usable. Empty is an ordinary result, not an
               error: a household that has never touched a scoped model may
               legitimately have no such limit, and the caller already raises
               when NOTHING at all was usable.
      Raises:  never. A malformed `limits[]` is a missing reading, not a failure
               of the top-level buckets that parsed fine beside it.

    `severity` IS STILL READ AND DISCARDED, here as everywhere else. The vendor
    grades its own numbers and NagLight grades ours; only one of them may own
    the panel's colour. This function reads `kind`, `scope.model.display_name`,
    `percent` and `resets_at`, and nothing else.

    Implements: LLR-005, SR-075 (IF-012 v1.1)
    """
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return {}
    out = {}
    for name, key, _label, window_seconds in scoped_models:
        matches = []
        for candidate in limits:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("kind") != CLAUDE_SCOPED_KIND:
                continue
            scope = candidate.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            if isinstance(model, dict) and model.get("display_name") == name:
                matches.append(candidate)
        if not matches:
            problems.append("no %s limit in limits[]" % name)
            continue
        # EVERY MATCH IS TRIED, NOT JUST THE FIRST. Nothing promises one entry
        # per model, and "first one wins" threw away a perfectly good second
        # reading whenever the first was malformed -- turning a recoverable
        # payload into a missing gauge. Found by adversarial review 2026-09-12.
        where = "%s scoped %s" % (source_name, name)
        for entry in matches:
            try:
                percent = check_percent(entry.get("percent"), where)
                resets_at = entry.get("resets_at")
                # A scoped weekly limit shares its reset with the all-model
                # weekly bucket, so this window is the same seven days -- but it
                # is read from the entry's OWN `resets_at` rather than copied
                # across, so a vendor that ever decouples them cannot make this
                # gauge lie.
                window_end, window_length = check_window(
                    None if resets_at is None
                    else parse_iso8601_utc(resets_at, where),
                    window_seconds, now, where)
            except SourceFailure as exc:
                problems.append(str(exc))
                continue
            out[key] = Reading(percent, window_end, window_length)
            break
    return out


# OpenCode's bucket names ARE the window lengths - except `rolling`, which
# names none and for which the endpoint supplies none. It is therefore absent
# from this table and is never posted: a window we would have had to invent is
# a fabricated reading, and `direction` is refused without a window anyway, so
# the alternative was a gauge with a 7-day staleness horizon that stays green
# for a week after the feeder dies. Recorded as a known gap in README.md.
# The bucket statuses this reader knows how to believe. An ALLOW-LIST, so a
# status nobody has seen yet still fails closed to "unavailable" rather than
# being read as a number.
#
#   ok            - an ordinary reading.
#   rate-limited  - the budget is SPENT. A definite statement, not a warning
#                   about the number's quality, and the single most useful
#                   thing this gauge can show.
OPENCODE_TRUSTED_STATUSES = frozenset({"ok", "rate-limited"})

# The monthly bucket is a CALENDAR month ending at `resetsAt`, so its length is
# derived from the reset (28-31 days) rather than fixed. A fixed 30 days put the
# start of every 31-day month in the future, and check_window refused the
# reading as "the window has not begun" for its first day - measured 2026-10-19
# on a fresh key whose month reset 2026-11-19T00:11:14Z. Every derived length is
# still "monthly" to window_kind_for (<= 32 days).
CALENDAR_MONTH = "calendar-month"

OPENCODE_BUCKETS = (
    ("weekly", "ai-usage-opencode-weekly", "OpenCode weekly", 7 * 24 * 3600),
    ("monthly", "ai-usage-opencode-monthly", "OpenCode monthly", CALENDAR_MONTH),
)


def calendar_month_seconds(window_end):
    """Length of the calendar month that ends at epoch second `window_end`.

    The start is the same UTC wall-clock instant one month earlier, its day
    clamped to that month's length (a month ending on 31 March starts on 28 or
    29 February). Nothing is assumed beyond the vendor's own reset instant.
    """
    end = datetime.fromtimestamp(int(window_end), timezone.utc)
    year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1]))
    return int(window_end) - int(start.timestamp())


def parse_opencode(payload, now):
    """Turn one Zen usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed
               ({"usage":{"weekly":{"status","percent","resetsAt"}, ...}});
               now: this cycle's clock, against which each window is checked.
      Outputs: dict of gauge key -> Reading.
      Raises:  SourceFailure on a non-object, a missing `usage`, or when no
               listed bucket is usable.

    A bucket whose status this reader does not KNOW is skipped rather than
    posted: an unrecognised status is the vendor telling us something we cannot
    interpret, and a number we cannot interpret must not become a gauge.

    `rate-limited` IS KNOWN, AND IT IS THE MOST IMPORTANT READING THERE IS.
    This used to require `status == "ok"` exactly, on the reasoning that "a
    number we were warned about must not become a green gauge" -- which does not
    survive contact with the real body. Measured 2026-09-12, the Owner's live
    monthly bucket was:

        "monthly": {"status": "rate-limited", "percent": 100, ...}

    That is not an untrustworthy number. It is the vendor stating, definitely,
    that the budget is spent -- and at 100 percent it could never have rendered
    green in the first place, because the gauge is full red at the far end. The
    old rule blanked the monthly bar to "unavailable" at exactly the moment it
    had something worth saying, while the Owner could see the limit plainly on
    the vendor's own site. A
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
        if status not in OPENCODE_TRUSTED_STATUSES:
            problems.append("%s status=%r" % (field, status))
            continue
        try:
            percent = check_percent(bucket.get("percent"), "opencode " + field)
            resets_at = bucket.get("resetsAt")
            end = None if resets_at is None else parse_iso8601_utc(resets_at, "opencode " + field)
            if window_seconds == CALENDAR_MONTH:
                window_seconds = None if end is None else calendar_month_seconds(end)
            window_end, window_seconds = check_window(end, window_seconds, now, "opencode " + field)
        except SourceFailure as exc:
            problems.append(str(exc))
            continue
        out[key] = Reading(percent, window_end, window_seconds)
    if not out:
        raise SourceFailure("opencode: no usable bucket (%s)" % "; ".join(problems))
    return out


# ── Gemini: the quota call Gemini CLI's own `/stats` makes ──────────────────
#
# SOURCE OF TRUTH, AND ITS LIMIT. Written from gemini-cli HEAD 9b6e026
# (2026-10-09) -- `packages/core/src/code_assist/types.ts` (RetrieveUserQuota*,
# BucketInfo, LoadCodeAssist*), `server.ts` (the two POSTs) and `config.ts`
# (`refreshUserQuota`, the only caller) -- and NOT from an observed response.
# Every other parser in this file was written after a real call; this one could
# not be, so it is deliberately stricter than they are: it accepts only the
# fields `types.ts` declares, refuses a window it cannot stand behind, and any
# surprise is "unavailable". README.md ("Gemini") lists what a first live run
# must confirm.
#
#   POST {base}:loadCodeAssist  {"metadata":{ideType,platform,pluginType:"GEMINI"}}
#        -> {"currentTier":{...},"cloudaicompanionProject":"<id>", ...}
#   POST {base}:retrieveUserQuota  {"project":"<id>"}
#        -> {"buckets":[{"modelId","remainingFraction","resetTime",
#                        "remainingAmount","tokenType"}, ...]}
#
# `remainingFraction` is REMAINING, 0..1. This file's Reading holds CONSUMED
# percent (the vendors' own convention elsewhere), so it is inverted here, once.
GEMINI_GAUGE_KEY = "ai-usage-gemini-daily"
GEMINI_LABEL_BASE = "Gemini daily"
# The plan's call ("Buckets are per model and probably daily"): a Gemini quota
# bucket is treated as a 24-hour window ending at its `resetTime`. That is an
# assumption, and `check_window` is what keeps it honest: a bucket whose reset
# is more than a day away (a weekly or monthly bucket) puts `now` outside the
# window it claims, and the whole reading is refused rather than mis-sized.
GEMINI_WINDOW_SECONDS = 24 * 3600
GEMINI_DEFAULT_BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"
_GEMINI_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_GEMINI_PROJECT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def parse_gemini_models(raw):
    """The configured model list, as a de-duplicated list of ids ([] = all).

    Raises SourceFailure on a malformed id: a typo that silently narrowed the
    gauge to nothing would be a gauge that never moves.
    """
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if not _GEMINI_MODEL_ID.match(item):
            raise SourceFailure(
                "gemini: AI_USAGE_GEMINI_MODELS has an entry that is not a model "
                "id (letters, digits, . _ - only, 64 max)")
        if item not in out:
            out.append(item)
    return out


def gemini_label(models):
    """The gauge label, which SAYS which bucket the number is.

    The plan's rule: one bucket stands for the whole provider (the lowest
    remaining fraction among the models in use), so the label has to name the
    set it is the minimum of. An overlong list falls back to a count -- never a
    silently truncated list, which would name fewer models than the number
    covers.
    """
    if not models:
        return "%s (all models)" % GEMINI_LABEL_BASE
    label = "%s (%s)" % (GEMINI_LABEL_BASE, ", ".join(models))
    if len(label) > RUNE_LIMIT:
        label = "%s (lowest of %d models)" % (GEMINI_LABEL_BASE, len(models))
    return label


def parse_gemini_project(payload, configured=None):
    """The project id out of a `loadCodeAssist` answer, or SourceFailure.

    The CLI's own rule (`setup.ts` `_doSetupUser`): an onboarded account carries
    `currentTier`, and the project is `cloudaicompanionProject` if the server
    names one, else the operator's own GOOGLE_CLOUD_PROJECT. An account with no
    `currentTier` is one the CLI would ONBOARD (`onboardUser`, a mutating call),
    and this feeder never makes one -- it refuses and says why.
    """
    if not isinstance(payload, dict):
        raise SourceFailure("gemini: loadCodeAssist answer is not an object")
    if not isinstance(payload.get("currentTier"), dict):
        raise SourceFailure(
            "gemini: loadCodeAssist returned no currentTier - this account has "
            "not been onboarded; run `gemini` once, signed in, as the feeder's "
            "account. The feeder never onboards an account itself.")
    project = payload.get("cloudaicompanionProject")
    if isinstance(project, str) and _GEMINI_PROJECT_ID.match(project):
        return project
    if project not in (None, ""):
        raise SourceFailure("gemini: loadCodeAssist named a project id of an "
                            "unexpected shape")
    if configured and _GEMINI_PROJECT_ID.match(configured):
        return configured
    raise SourceFailure(
        "gemini: no project id - loadCodeAssist named none and "
        "AI_USAGE_GEMINI_PROJECT is unset (the CLI needs GOOGLE_CLOUD_PROJECT "
        "for this account type too)")


def parse_gemini_quota(payload, now, models=()):
    """Turn one `retrieveUserQuota` answer into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body; now: this cycle's clock;
               models: the configured model ids, or () for every bucket.
      Outputs: {GEMINI_GAUGE_KEY: Reading} for the bucket with the LOWEST
               remainingFraction (the most-consumed one), window = the 24 h
               ending at that bucket's own `resetTime`.
      Raises:  SourceFailure on a non-object, no `buckets`, no usable bucket, a
               configured model with no bucket, and a bucket whose window
               `check_window` refuses.

    A bucket with no `modelId` or no numeric `remainingFraction` is skipped, as
    the CLI itself skips it (`config.ts` refreshUserQuota). Every OTHER fault
    is fatal to the reading, and that is deliberate: the answer is a minimum, so
    quietly dropping a malformed bucket could drop the very one that was lowest
    and report headroom that is not there.

    Implements: LLR-1049
    """
    if not isinstance(payload, dict):
        raise SourceFailure("gemini: quota answer is not an object")
    buckets = payload.get("buckets")
    if not isinstance(buckets, list):
        raise SourceFailure("gemini: quota answer has no buckets list")
    pool = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        model = bucket.get("modelId")
        fraction = bucket.get("remainingFraction")
        if not isinstance(model, str) or not model:
            continue
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            continue
        if not _GEMINI_MODEL_ID.match(model):
            raise SourceFailure("gemini: a bucket names a model id of an "
                                "unexpected shape")
        if not math.isfinite(fraction) or fraction < 0.0 or fraction > 1.0:
            raise SourceFailure("gemini: %s remainingFraction outside 0..1" % model)
        pool.append((model, float(fraction), bucket.get("resetTime")))
    if not pool:
        raise SourceFailure("gemini: quota answer carries no usable bucket")
    returned = sorted(set(m for m, _f, _r in pool))
    if models:
        missing = [m for m in models if m not in returned]
        if missing:
            raise SourceFailure(
                "gemini: no quota bucket for configured model(s) %s; the vendor "
                "returned: %s" % (", ".join(missing), ", ".join(returned[:12])))
        pool = [entry for entry in pool if entry[0] in models]
    # Validate EVERY candidate's window before choosing (see the docstring).
    checked = []
    for model, fraction, reset in pool:
        where = "gemini " + model
        end, length = check_window(
            None if reset is None else parse_iso8601_utc(reset, where),
            GEMINI_WINDOW_SECONDS, now, where)
        checked.append((fraction, end, model, length))
    fraction, end, _model, length = min(checked)
    consumed = min(max((1.0 - fraction) * 100.0, 0.0), 100.0)
    return {GEMINI_GAUGE_KEY: Reading(check_percent(consumed, "gemini"), end, length)}


GAUGE_SPECS = (
    GaugeSpec("ai-usage-codex", "Codex weekly", "🤖", "codex"),
    GaugeSpec("ai-usage-claude-session", "Claude session", "🧠", "claude"),
    GaugeSpec("ai-usage-claude-weekly", "Claude weekly", "🧠", "claude"),
    # The scoped per-model gauge shares Claude's icon, so the panel groups it
    # with the other two rather than inventing a fourth provider. Its id sorts
    # immediately AFTER `ai-usage-claude-weekly`, which matters: NagLight serves
    # gauges in id order, and the panel draws the first gauge of a window as the
    # headline and a second one sharing that window as the narrow sub-column.
    # Renaming this key to sort before the all-model weekly would silently swap
    # which of the two is the headline.
    GaugeSpec("ai-usage-claude-weekly-fable", "Claude F5", "🧠", "claude",
              optional=True),
    GaugeSpec("ai-usage-opencode-weekly", "OpenCode weekly", "🧩", "opencode"),
    GaugeSpec("ai-usage-opencode-monthly", "OpenCode monthly", "🧩", "opencode"),
    # Account 2 shares account 1's icon on purpose: the panel groups by the
    # provider id in the gauge id (`claude2`), not by icon, so the icon only
    # selects the brand mark. The id order is the same load-bearing one as
    # account 1's: `-weekly-fable` sorts immediately after `-weekly`.
    GaugeSpec("ai-usage-claude2-session", "Claude 2 session", "🧠", "claude2"),
    GaugeSpec("ai-usage-claude2-weekly", "Claude 2 weekly", "🧠", "claude2"),
    GaugeSpec("ai-usage-claude2-weekly-fable", "Claude 2 F5", "🧠", "claude2",
              optional=True),
    # The sparkle is the nearest emoji to Gemini's mark; the panel keys its
    # brand marks on the provider id in the gauge id (`gemini`) or on this
    # emoji, whichever the panel builder bundles.
    GaugeSpec(GEMINI_GAUGE_KEY, gemini_label(()), "✨", "gemini"),
)


def gauge_specs(env):
    """GAUGE_SPECS with the Gemini label naming the configured models.

    Never raises: a malformed model list leaves the default label, and the
    reader reports the malformed list as the failure it is.
    """
    try:
        label = gemini_label(parse_gemini_models(env.get("AI_USAGE_GEMINI_MODELS")))
    except SourceFailure:
        return GAUGE_SPECS
    return tuple(
        GaugeSpec(spec.key, label, spec.icon, spec.source, spec.optional)
        if spec.key == GEMINI_GAUGE_KEY else spec
        for spec in GAUGE_SPECS)


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
        # CONSUMED IN, REMAINING OUT, AND THE INVERSION LIVES HERE ONLY.
        # Every vendor states usage as percent CONSUMED, and that is what the
        # readers parse, what `Reading.percent` holds and what the state file
        # stores -- so the number we keep is the number the source actually
        # said. The wire wants remaining (see GAUGE_DIRECTION), and this is the
        # single point where the body is assembled, so it is the single point
        # that flips. Both callers in `build_post` pass a CONSUMED percent --
        # this cycle's reading, or a stored one being re-posted at its original
        # timestamp -- so both get inverted identically and no stored state has
        # to be migrated.
        "value": float(GAUGE_MAX - value),
        "min": GAUGE_MIN,
        "max": GAUGE_MAX,
        "target": GAUGE_TARGET,
    }
    if window_end is not None and window_seconds is not None:
        body["window"] = {
            "start": rfc3339_utc(int(window_end) - int(window_seconds)),
            "end": rfc3339_utc(int(window_end)),
            "kind": window_kind_for(window_seconds),
        }
        body["direction"] = GAUGE_DIRECTION
    if observed_at is not None:
        body["observed_at"] = rfc3339_utc(int(observed_at))
    return body


def rfc3339_utc(epoch_seconds):
    """Epoch seconds -> the RFC3339 UTC string the feed contract asks for.

    THESE THREE FIELDS WERE EPOCH INTEGERS AND THE TRACKER REFUSED EVERY BODY.
    IF-012 says `window{start,end}` and `observed_at` are RFC3339, and NagLight
    declares them as Go `string`s (internal/gauge/gauge.go: "Start and End are
    RFC3339"), so an integer is not a loose interpretation of the contract -- it
    does not unmarshal at all. Measured on the hub 2026-09-12, posting a real
    OpenCode reading:

        HTTP 400 -> bad JSON body: json: cannot unmarshal number into Go struct
        field Window.window.start of type string

    Every gauge this feeder has ever built carried them, so the AI-usage half
    could never have posted a single gauge -- which went unnoticed because the
    feeder had never been run against a live tracker before today, and because
    its own failure path turns a refused POST into exactly the "unavailable"
    gauge a missing credential produces.

    UTC with a `Z` designator, seconds precision, and no microseconds: the
    tracker parses RFC3339 and compares against its own clock, so an offsetless
    local string would be read as the wrong instant.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch_seconds)))


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


# ── Per-source cadence, backoff and authentication history (W11) ─────────────
#
# THE TIMER RUNS EVERY MINUTE (Owner 2026-10-22: "update the AI usage gauges
# every minute; they fall behind"), but that does not mean every source is read
# every minute. Each source -- and each Claude ACCOUNT, because `claude` and
# `claude2` are separate sources -- carries its own schedule, and a cycle SKIPS a
# source that is not due or is backing off. This is a one-shot unit, so nothing
# in memory survives a cycle: the schedule lives in the state file under one
# reserved key, SCHEDULE_KEY, with its own validator in `load_state` (never an
# exemption from validation), exactly as the auth marker does.
#
#   SCHEDULE_KEY      {source: entry}. "Reserved" because no gauge id can start
#                     with an underscore (they are all `ai-usage-...`).
#   an AUTH MARKER    under a GAUGE's own key, REPLACING its stored reading once
#                     an authentication failure is confirmed:
#                     {"auth_failed": true, "at": <epoch>}.
#
# One schedule entry, every field a number, a flag or a word from a fixed list:
#
#   last_attempt   when the source was last actually READ (a skip is not one)
#   next_due       when it may be read again
#   hold           True when next_due is the VENDOR's Retry-After: then it is
#                  honoured to the second, with no DUE_SLACK_SECONDS
#   backoff        consecutive throttled attempts (429 / 5xx); 0 when healthy
#   auth_since     the first failed-authentication attempt of the current run
#   auth_attempts  how many failed-authentication attempts that run holds
#   last_outcome   one of OUTCOMES
#   ratelimit      the vendor's rate-limit response headers, last seen (optional)
#
# THE MARKER REPLACES THE READING AND DOES NOT DELETE THE KEY, and that is the
# whole point of its shape. `state_keys` answers "have we ever seen this gauge",
# and an OPTIONAL gauge (Claude's Fable band) is skipped when its key is absent.
# Deleting the key would make the band VANISH instead of going grey -- the very
# defect `state_keys`'s docstring records from the 2026-09 review.
#
# Both hold numbers, flags and the vendor's rate-limit header values only: never
# a credential, a request header or a body.
SCHEDULE_KEY = "__schedule__"

# The words an attempt can end in. A SKIP is deliberately not one of them: a
# skipped source was not attempted, so its entry is left exactly as it was.
OUTCOMES = ("ok", "auth", "throttled", "failure", "pending")

# How often each source is read when it is healthy. 60 s for the HTTP sources
# (the Owner's minute). Codex is the exception, chosen by measurement: one read
# spawns `codex app-server`, which cost 2.4-2.9 s of CPU per read on the hub
# (2026-10-22, per-process accounting of the unit's cgroup) against ~0.5 s for
# the whole rest of the cycle, so reading it every minute would be ~4 % of one
# core for a WEEKLY gauge. Every second cycle halves that and still meets the
# 3-minute freshness target (README "Cadence"). A long-lived app-server was the
# alternative; see README for why it was not chosen.
DEFAULT_SOURCE_INTERVAL_SECONDS = 60
SOURCE_INTERVAL_DEFAULTS = {"codex": 120}
# A knob outside these bounds is refused. Below 60 s the timer cannot honour it;
# above 600 s is slower than the cadence this feeder had before 2026-10-22, and
# the status step's evidence window (ai-cli-status.sh) is twice that.
SOURCE_INTERVAL_MIN_SECONDS = 60
SOURCE_INTERVAL_MAX_SECONDS = 600

# A healthy source is due this much BEFORE its next_due, because the timer is
# not a metronome: OnUnitActiveSec=1min plus up to RandomizedDelaySec=10s and
# AccuracySec=5s puts consecutive cycles 60-75 s apart, and without slack a
# source due at +60 s that the next cycle reaches at +59 s would wait a whole
# extra cycle. It must exceed the timer's own jitter and stay well under the
# shortest interval; tests/test_ai_usage_feeder.py asserts both against the
# shipped timer. It never applies to a vendor's Retry-After (`hold`).
DUE_SLACK_SECONDS = 15

# Exponential backoff after a 429 or 5xx that named no Retry-After: the
# source's interval doubled per consecutive throttled attempt, to this ceiling.
BACKOFF_CEILING_SECONDS = 600
_BACKOFF_LEVEL_CEILING = 30

# A Retry-After LONGER than the backoff ceiling is honoured as given -- the
# vendor knows its own limit -- up to this sanity bound, past which the value is
# clamped to it rather than parked for a year by one malformed header.
RETRY_AFTER_MAX_SECONDS = 24 * 3600

# A source's gauges go grey only after failed-AUTHENTICATION attempts that number
# at least AUTH_CONFIRM_ATTEMPTS and span at least AUTH_CONFIRM_SECONDS. The rule
# used to be "two consecutive cycles", written for a 10-minute timer; on a
# 1-minute timer that would grey a gauge two minutes into any blip. Measured on
# the hub (journal 2026-10-12..21): every Claude 401 since the 10-11 re-sign-in
# was the access token lapsing shortly before homehub-claude-refresh@ renewed it,
# and each cleared on the next refresher run. The span is what absorbs that
# window; the refresher's cadence is what keeps it short (its timer's comment).
AUTH_CONFIRM_ATTEMPTS = 2
AUTH_CONFIRM_SECONDS = 600
_AUTH_ATTEMPT_CEILING = 100000

# What a rate-limit response header may be called, and look like, to be recorded.
_RATE_LIMIT_HEADER = re.compile(
    r"(?i)^(retry-after|x-ratelimit-[a-z0-9-]{1,48}|x-rate-limit-[a-z0-9-]{1,48}"
    r"|ratelimit(-[a-z0-9-]{1,48})?|anthropic-ratelimit-[a-z0-9-]{1,48})$")
_RATE_LIMIT_VALUE = re.compile(r"^[A-Za-z0-9 ,.:;=_+/-]{0,64}$")
_RATE_LIMIT_MAX_HEADERS = 16


def _epoch_int(raw):
    """An integer epoch from a JSON number, or None (bools, NaN and strings are not)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None
    return int(raw)


def sanitize_rate_limit_headers(pairs):
    """{name: value} of the rate-limit headers among `pairs`, made safe to store.

    Contract:
      Inputs:  pairs: an iterable of (name, value) -- an HTTP response's headers.
      Outputs: at most _RATE_LIMIT_MAX_HEADERS entries, names lower-cased and
               matching _RATE_LIMIT_HEADER, values matching _RATE_LIMIT_VALUE.
               Anything else is dropped, never repaired.

    These are the vendor's numbers about its own limits (W11 task 2: "record each
    source's rate-limit response headers when present"). The allow-list of NAMES
    keeps every other header -- a cookie, a request id -- out of the state file;
    the value pattern keeps anything token-shaped out even under an allowed name.

    Implements: SR-095, LLR-1192
    """
    out = {}
    for name, value in pairs or ():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        name = name.strip().lower()
        value = value.strip()
        # fullmatch, not match: `$` would let one trailing newline through.
        if _RATE_LIMIT_HEADER.fullmatch(name) and _RATE_LIMIT_VALUE.fullmatch(value):
            out[name] = value
            if len(out) >= _RATE_LIMIT_MAX_HEADERS:
                break
    return out


def parse_retry_after(value, now):
    """A Retry-After header -> whole seconds to wait from `now`, or None.

    Contract:
      Inputs:  value: the header's text, or None; now: epoch seconds.
      Outputs: int in 0..RETRY_AFTER_MAX_SECONDS, or None when the value is
               absent or is neither form (the caller then backs off
               exponentially).

    BOTH FORMS (RFC 9110 10.2.3): delay-seconds (`120`) and an HTTP-date
    (`Wed, 21 Oct 2026 07:28:00 GMT`). A date already past is 0 -- come back
    now -- not a refusal. A value longer than BACKOFF_CEILING_SECONDS is
    honoured as given; only one past RETRY_AFTER_MAX_SECONDS is clamped.

    Implements: SR-095, LLR-1192
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int(math.ceil(when.timestamp() - now))
    return max(0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def validate_schedule(entry, sources, now):
    """The per-source schedule, or {} for anything that is not credible.

    Contract:
      Inputs:  entry: whatever the state file holds under SCHEDULE_KEY;
               sources: the source names that exist (SOURCE_READERS);
               now: this cycle's clock.
      Outputs: {source: entry} holding only entries whose every field passes;
               an entry with one bad field is dropped WHOLE, never repaired.
               A dropped entry means "due now, no history", which is the safe
               reading of a schedule nobody can vouch for.

    A STAMP FROM THE FUTURE IS A CLOCK THAT WENT BACKWARDS, not a schedule. An
    entry whose last_attempt lies past `now` (plus MAX_CLOCK_SKEW_SECONDS) is
    dropped, so a hub whose clock steps back a day reads the source at once
    instead of skipping it for a day. A clock that steps FORWARD needs no rule:
    every next_due is simply in the past, and the source is due.

    Implements: SR-095, LLR-1190
    """
    if not isinstance(entry, dict):
        return {}
    out = {}
    for source, item in entry.items():
        if source in sources:
            checked = _validate_schedule_entry(item, now)
            if checked is not None:
                out[source] = checked
    return out


def _validate_schedule_entry(item, now):
    if not isinstance(item, dict):
        return None
    last = _epoch_int(item.get("last_attempt"))
    due = _epoch_int(item.get("next_due"))
    if last is None or due is None:
        return None
    if last < EPOCH_FLOOR or last > now + MAX_CLOCK_SKEW_SECONDS:
        return None
    if not (last <= due <= last + RETRY_AFTER_MAX_SECONDS + MAX_CLOCK_SKEW_SECONDS):
        return None
    hold = item.get("hold")
    backoff = item.get("backoff")
    outcome = item.get("last_outcome")
    if not isinstance(hold, bool) or outcome not in OUTCOMES:
        return None
    if isinstance(backoff, bool) or not isinstance(backoff, int) \
            or not 0 <= backoff <= _BACKOFF_LEVEL_CEILING:
        return None
    out = {"last_attempt": last, "next_due": due, "hold": hold,
           "backoff": backoff, "last_outcome": outcome}
    since, attempts = item.get("auth_since"), item.get("auth_attempts")
    if since is not None or attempts is not None:
        since = _epoch_int(since)
        if since is None or not EPOCH_FLOOR <= since <= last:
            return None
        if isinstance(attempts, bool) or not isinstance(attempts, int) \
                or not 1 <= attempts <= _AUTH_ATTEMPT_CEILING:
            return None
        out["auth_since"], out["auth_attempts"] = since, attempts
    if "ratelimit" in item:
        headers = item["ratelimit"]
        if not isinstance(headers, dict):
            return None
        clean = sanitize_rate_limit_headers(headers.items())
        if len(clean) != len(headers):
            return None
        if clean:
            out["ratelimit"] = clean
    return out


def source_intervals(env, names):
    """{source: seconds between reads} for `names`, from the per-source knobs.

    Contract:
      Config:  AI_USAGE_<SOURCE>_INTERVAL (seconds, an integer), e.g.
               AI_USAGE_CODEX_INTERVAL. Blank or absent is the default
               (SOURCE_INTERVAL_DEFAULTS, else DEFAULT_SOURCE_INTERVAL_SECONDS).
      Raises:  SystemExit naming the knob when a value is not an integer in
               SOURCE_INTERVAL_MIN_SECONDS..SOURCE_INTERVAL_MAX_SECONDS. A
               malformed knob is a refusal, the same as an unknown source in
               AI_USAGE_SOURCES; `--check` (the unit's ExecStartPre) runs it.

    Implements: SR-095, LLR-1190
    """
    out = {}
    for name in names:
        knob = "AI_USAGE_%s_INTERVAL" % name.upper()
        raw = (env.get(knob) or "").strip()
        if not raw:
            out[name] = SOURCE_INTERVAL_DEFAULTS.get(name, DEFAULT_SOURCE_INTERVAL_SECONDS)
            continue
        if not raw.isdigit() or not (SOURCE_INTERVAL_MIN_SECONDS <= int(raw)
                                     <= SOURCE_INTERVAL_MAX_SECONDS):
            raise SystemExit(
                "REFUSED: %s=%r must be a whole number of seconds from %d to %d"
                % (knob, raw, SOURCE_INTERVAL_MIN_SECONDS, SOURCE_INTERVAL_MAX_SECONDS))
        out[name] = int(raw)
    return out


def schedule_wait(entry, now):
    """Seconds until this source may be read again; 0 means read it this cycle.

    A source with no (credible) entry is due. A healthy source is due
    DUE_SLACK_SECONDS early (the timer's jitter); a vendor's Retry-After
    (`hold`) is honoured to the second.

    Implements: SR-095, LLR-1190
    """
    if not entry:
        return 0
    slack = 0 if entry["hold"] else DUE_SLACK_SECONDS
    return max(0, entry["next_due"] - slack - now)


def next_schedule(entry, outcome, now, interval, retry_after=None, ratelimit=None):
    """The schedule entry after ONE ATTEMPT that ended in `outcome`. Pure.

    Contract:
      Inputs:  entry: the validated previous entry, or None;
               outcome: one of OUTCOMES; now: this cycle's clock;
               interval: the source's healthy cadence in seconds;
               retry_after: seconds from `parse_retry_after`, for "throttled";
               ratelimit: sanitized rate-limit headers seen on this attempt.
      Outputs: a new entry; `entry` is never mutated.

    The rules, each one a line of W11 task 2 or 4:
      ok         due again after `interval`; backoff and auth history cleared
                 (A SUCCESS RESETS).
      auth       due again after `interval` (a dead login is not throttling);
                 the auth run's first attempt is kept and its count advanced.
      throttled  the vendor's Retry-After when it sent one (strict, `hold`, when
                 it asks for longer than our own cadence would wait anyway), else
                 `interval` doubled per consecutive throttled attempt up to
                 BACKOFF_CEILING_SECONDS. Auth history untouched.
      failure /  due again after `interval`; backoff cleared; auth history
      pending    untouched -- the vendor never answered about the login, so the
                 attempt is evidence neither way.
    A SKIPPED cycle never reaches here, which is why a skip neither resets nor
    advances anything.

    Implements: SR-095, SR-096, LLR-1191, LLR-1192, LLR-1193
    """
    if outcome not in OUTCOMES:
        raise ValueError("unknown outcome %r" % (outcome,))
    previous = entry or {}
    out = {"last_attempt": now, "next_due": now + interval, "hold": False,
           "backoff": 0, "last_outcome": outcome}
    if outcome == "throttled":
        level = min(previous.get("backoff", 0) + 1, _BACKOFF_LEVEL_CEILING)
        out["backoff"] = level
        if retry_after is not None:
            out["next_due"] = now + max(retry_after, interval)
            out["hold"] = retry_after > interval - DUE_SLACK_SECONDS
        else:
            ceiling = max(BACKOFF_CEILING_SECONDS, interval)
            out["next_due"] = now + min(interval * (2 ** level), ceiling)
    if outcome != "ok" and "auth_since" in previous:
        out["auth_since"] = previous["auth_since"]
        out["auth_attempts"] = previous["auth_attempts"]
    if outcome == "auth":
        out.setdefault("auth_since", now)
        out["auth_attempts"] = min(out.get("auth_attempts", 0) + 1, _AUTH_ATTEMPT_CEILING)
    headers = ratelimit or previous.get("ratelimit")
    if headers:
        out["ratelimit"] = dict(headers)
    return out


def auth_failure_confirmed(entry):
    """True once the source's failed-authentication attempts are CONFIRMED.

    At least AUTH_CONFIRM_ATTEMPTS attempts, spanning at least
    AUTH_CONFIRM_SECONDS from the first to the latest. Only attempts count: a
    skipped cycle adds neither an attempt nor any span. The entry is the one
    `next_schedule` just wrote, so its last_attempt IS the latest attempt.

    Implements: SR-068, LLR-1054
    """
    if not entry or "auth_since" not in entry:
        return False
    return (entry["auth_attempts"] >= AUTH_CONFIRM_ATTEMPTS
            and entry["last_attempt"] - entry["auth_since"] >= AUTH_CONFIRM_SECONDS)


# ── The journal's words for the schedule. Kept stable: the 24-hour verification
# (README "Verifying the cadence") greps for them.

def skip_note(name, entry, wait, interval):
    """The INFO line for a skipped source: why, and for how long.

    Implements: SR-095, LLR-1190
    """
    if entry and entry.get("last_outcome") == "throttled":
        return "%s: skipped - backing off, %ds left (level %d%s)" % (
            name, wait, entry["backoff"], ", Retry-After" if entry["hold"] else "")
    return "%s: skipped - not due for %ds (reads every %ds)" % (name, wait, interval)


def backoff_line(name, entry, now, retry_after):
    """The failure line that says a source has just started (or extended) a backoff.

    Implements: SR-095, LLR-1192
    """
    delay = entry["next_due"] - now
    reason = ("Retry-After %ds honoured" % retry_after if retry_after is not None
              else "no Retry-After, exponential to %ds" % max(BACKOFF_CEILING_SECONDS, delay))
    return "%s: backing off %ds (level %d, %s)" % (name, delay, entry["backoff"], reason)


def auth_line(name, entry):
    """The failure line for a failed-authentication attempt: confirmed or not.

    "authentication failure" and "CONFIRMED" are the words the refresh-gauge-logins
    skill and the old two-cycle rule's journal both used, so a grep written for
    either still finds these.

    Implements: SR-068, LLR-1054
    """
    span = entry["last_attempt"] - entry["auth_since"]
    if auth_failure_confirmed(entry):
        tail = " - CONFIRMED, its gauges are posted unavailable"
    else:
        tail = " - last reading kept until confirmed (%d attempts over %ds)" % (
            AUTH_CONFIRM_ATTEMPTS, AUTH_CONFIRM_SECONDS)
    return "%s: authentication failure, attempt %d over %ds%s" % (
        name, entry["auth_attempts"], span, tail)


def validate_auth_marker(entry, now):
    """An "auth failed" marker, or None. Its own shape; see SCHEDULE_KEY.

    Implements: SR-068, LLR-1055
    """
    if not isinstance(entry, dict) or entry.get("auth_failed") is not True:
        return None
    stamp = entry.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) \
            or not math.isfinite(stamp):
        return None
    stamp = int(stamp)
    if stamp < EPOCH_FLOOR or stamp > now + MAX_CLOCK_SKEW_SECONDS:
        return None
    return {"auth_failed": True, "at": stamp}


def build_post(spec, reading, last, now, auth_confirmed=False):
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
               now:     epoch seconds;
               auth_confirmed: True when this gauge's source has failed
                        authentication on enough attempts over enough time to
                        be CONFIRMED dead (plan 3.6, G2; W11
                        `auth_failure_confirmed`). The stored reading is then
                        not history any more: it is skipped and the
                        stale-on-arrival body goes out.
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
        displayed value by construction;
      * source failed AUTHENTICATION, confirmed over attempts and time (`auth_confirmed`)
        -> the never-measured body even though a previous reading exists. A
        dead login must not keep its last number in colour until the horizon.
        A stored AUTH MARKER (`validate_auth_marker`) is not a reading, so it
        falls through `validate_stored_reading` to the same body -- which is
        what keeps the result sticky when a later cycle fails some other way.

    Implements: SR-021, LLR-005, LLR-1055
    """
    if reading is not None:
        return build_gauge(spec, reading.percent, now,
                           reading.window_end, reading.window_seconds), True
    checked = None if auth_confirmed else validate_stored_reading(last, now)
    if checked is not None:
        return build_gauge(spec, checked["value"], checked["observed_at"],
                           checked["window_end"], checked["window_seconds"]), False
    return build_gauge(spec, UNAVAILABLE_CONSUMED, None, None, None), False


def is_fresh(body, now):
    """True if NagLight would still consider this body's reading current.

    Used by the tests to assert the "never a green gauge" property directly
    against NagLight's horizon table rather than against our own intent: a
    body with no `observed_at`, or one older than the horizon for its window
    kind, or one stamped in the future, is not fresh.

    Implements: LLR-005
    """
    # The BODY carries the wire format (RFC3339), not epoch seconds, so this
    # reads it back rather than comparing a string to a number. It used to
    # compare directly, which worked only because the body was wrong: the
    # tracker declares these fields as strings and refused every integer.
    stamp = epoch_from_rfc3339(body.get("observed_at"))
    if stamp is None:
        return False
    if stamp > now:
        return False        # a future stamp is stale by NagLight's rule.
    kind = body.get("window", {}).get("kind")
    return (now - stamp) <= STALE_HORIZON_SECONDS.get(kind, 24 * 3600)


def epoch_from_rfc3339(text):
    """The inverse of `rfc3339_utc`, or None for anything unparseable.

    Deliberately narrow: it reads back exactly what this feeder writes. A body
    is only ever inspected by `is_fresh`, which must fail CLOSED -- an
    unreadable stamp is "not fresh", never "fresh", so a malformed value can
    never make a dead source look alive.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


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

    TWO KINDS OF ENTRY BESIDE A READING ARE RECOGNISED, each by its own
    validator and never by an exemption from validation: the reserved
    SCHEDULE_KEY (each source's next due time, backoff level and
    authentication history) and an auth marker under a gauge's key. Without
    that, the loader would silently drop them and the next `save_state` would
    erase the schedule and the marker with them -- which is exactly how a
    one-shot unit would forget it had ever failed, or been told to back off.

    A state file written before 2026-10-22 may hold `__auth_failures__`, the old
    consecutive-cycle counter. It is not a reading and has no validator, so it
    is dropped like any other junk: the authentication history restarts, and a
    gauge already marked stays marked.
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
        if key == SCHEDULE_KEY:
            schedule = validate_schedule(entry, SOURCE_READERS, now)
            if schedule:
                out[key] = schedule
            continue
        checked = validate_stored_reading(entry, now)
        if checked is None:
            checked = validate_auth_marker(entry, now)
        if checked is not None:
            out[key] = checked
    return out


def state_keys(state_path):
    """Every gauge key the state file MENTIONS, valid or not.

    `load_state` drops an entry it cannot vouch for, which is right for reading
    a value and wrong for answering "have we ever seen this gauge". An OPTIONAL
    spec is skipped when nothing has ever been said about it, and with only the
    validated dict to go on a CORRUPT entry looked identical to a missing one --
    so a Fable gauge that had been posted for weeks would silently vanish from
    the wall the first cycle its stored reading went bad, instead of showing as
    unavailable. Found by adversarial review 2026-09-12.

    Presence, not content: a key here means "this gauge is real and we have
    posted it", nothing more.
    """
    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, dict) else set()


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
    except (FileNotFoundError, NotADirectoryError) as exc:
        if path_absent(os.path.expanduser(path)):
            raise PendingState("login owed: no credential at %s yet" % path)
        raise SourceFailure("credential %s unreadable: %s" % (path, type(exc).__name__))
    except (OSError, ValueError) as exc:
        raise SourceFailure("credential %s unreadable: %s" % (path, type(exc).__name__))
    value = extractor(data)
    if not value:
        raise NoUsableKey("credential %s carries no usable key" % path)
    return value


class NoUsableKey(SourceFailure):
    """The credential file parsed but held nothing the extractor could use.

    A PLAIN SourceFailure to every caller except one. The same text once came
    from a renamed OpenCode schema (`_opencode_key`) -- a perfectly good
    credential read wrongly -- so it must never be classified as authentication
    by its words. `read_claude` alone promotes it to an AuthFailure, because
    for Claude a file that parses but carries no access token IS what an
    expired or half-rewritten login looks like.
    """


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


# The provider id the CLI writes, newest spelling first. `opencode-go` is what
# the 2026-09-09 verification file carried and what this reader was built from;
# opencode 1.18.30 writes plain `opencode`. Measured on the hub 2026-09-12: a
# real `opencode auth login` produced {"opencode": {"type": "api", "key": ...}}
# and this reader answered "carries no usable key" against a credential that was
# perfectly good -- an "unavailable" gauge caused entirely by a renamed key.
#
# Both spellings are accepted rather than the old one replaced: a household that
# signed in on the older CLI still has the old file and must not be broken to
# fix the new one.
_OPENCODE_PROVIDER_IDS = ("opencode-go", "opencode")


def _opencode_key(data):
    if not isinstance(data, dict):
        return None
    for provider in _OPENCODE_PROVIDER_IDS:
        entry = data.get(provider)
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"]:
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


# Where `http_json` puts the rate-limit headers it sees, for the source being
# read right now. `run_cycle` sets a fresh list around each reader and resets it
# after; outside a cycle (a test, a hand call) it is None and nothing is kept.
# A context variable rather than a parameter so that no reader signature and no
# test double of `http_json` had to change to carry it.
_RATE_LIMIT_SINK = contextvars.ContextVar("ai_usage_rate_limit_sink", default=None)


def record_rate_limit_headers(headers):
    """Sanitize a response's rate-limit headers and hand them to the cycle's sink.

    Returns the sanitized dict (possibly empty) so a caller can also use it.
    `headers` is an http.client message or anything with `.items()`; None and
    an unreadable object are no headers, never an exception.

    Implements: SR-095, LLR-1192
    """
    try:
        pairs = list(headers.items()) if headers is not None else []
    except Exception:
        pairs = []
    seen = sanitize_rate_limit_headers(pairs)
    sink = _RATE_LIMIT_SINK.get()
    if seen and sink is not None:
        sink.append(seen)
    return seen


def throttled_failure(message, status, seen, now):
    """A `Throttled` for a 429/5xx whose sanitized headers are `seen`.

    The message names the Retry-After as parsed and every recorded rate-limit
    header, so the journal says WHY the source is backing off with the vendor's
    own numbers -- and nothing else the vendor sent.

    Implements: SR-095, LLR-1192
    """
    retry_after = parse_retry_after(seen.get("retry-after"), now)
    if seen:
        message += " (rate-limit headers: %s)" % ", ".join(
            "%s=%s" % pair for pair in sorted(seen.items()))
    return Throttled(message, status=status, retry_after=retry_after)


def http_json(url, headers, timeout, data=None):
    """One GET (or, with `data`, one JSON POST), decoded.

    Every failure class becomes SourceFailure. Transport error, timeout,
    non-200 and an undecodable body are ONE outcome on purpose - see
    SourceFailure - with ONE exception: an HTTP 401 is an `AuthFailure`
    (carrying `status`), because it is the vendor saying the login is no good,
    and `run_cycle` must be able to tell that from a 500. The response body is
    NOT echoed into the exception on an auth failure, because a 401 body can
    carry request context.

    The URL must be https. These headers carry a vendor bearer token, and the
    URL is a knob (AI_USAGE_CLAUDE_URL / AI_USAGE_OPENCODE_URL); an http one
    would put the household's token on the wire in clear. The refusal is here
    rather than at the knob because this is the one function that attaches the
    token to a request.

    `data` (a JSON-encodable object) turns the call into a POST with
    `Content-Type: application/json`. Gemini's two calls are POSTs; nothing
    else about the opener changes -- same redirect refusal, same no-proxy rule.

    A 429 or a 5xx is a `Throttled` failure carrying the parsed Retry-After
    (W11), so `run_cycle` backs the source off instead of asking again next
    minute. The vendor's rate-limit headers, on any answer, go to the cycle's
    sink (`record_rate_limit_headers`); the signature is unchanged on purpose,
    so every reader and every test double keeps working.
    """
    if not url.lower().startswith("https://"):
        raise SourceFailure(
            "%s: refused - a vendor URL carrying a bearer token must be https"
            % url)
    if data is None:
        request = urllib.request.Request(url, headers=headers)
    else:
        request = urllib.request.Request(
            url, data=json.dumps(data).encode("utf-8"), method="POST",
            headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with vendor_opener().open(request, timeout=timeout) as response:
            record_rate_limit_headers(response.headers)
            if response.status != 200:
                raise SourceFailure("%s: HTTP %s" % (url, response.status))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        seen = record_rate_limit_headers(exc.headers)
        if exc.code == 401:
            raise AuthFailure("%s: HTTP 401" % url, status=401)
        if exc.code == 429 or 500 <= exc.code <= 599:
            raise throttled_failure("%s: HTTP %s" % (url, exc.code), exc.code, seen,
                                    int(time.time()))
        raise SourceFailure("%s: HTTP %s" % (url, exc.code))
    except (SourceFailure, EgressRefused):
        raise
    except Exception as exc:
        raise SourceFailure("%s: %s" % (url, type(exc).__name__))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SourceFailure("%s: body is not JSON" % url)


# What a REMOTE-chosen string may look like after redaction. Order matters: the
# specific shapes first, so a JWT is named as one before the generic rule eats
# part of it.
_REDACTIONS = (
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-.]{8,}"), "[redacted]"),          # JWT
    (re.compile(r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|ya29|xox[a-z])[-_.][A-Za-z0-9_\-.]{8,}"),
     "[redacted]"),                                                    # vendor key prefixes
    (re.compile(r"[A-Za-z0-9_\-+=]{24,}"), "[redacted]"),             # any long opaque run
)
_REMOTE_TEXT_LIMIT = 200


def redact_remote_text(text, limit=_REMOTE_TEXT_LIMIT):
    """A vendor's free text made safe to put in the journal: redacted, then cut.

    REDACT FIRST, THEN TRUNCATE. Cutting first could slice a token in half and
    leave a prefix that no pattern recognises. A run of 24+ characters from the
    base64/base64url/hex alphabet is replaced whatever it looks like: a message
    has no legitimate reason to contain one, and a false positive costs a word
    of diagnostic where a miss costs a credential. Control characters are
    flattened so a hostile server cannot forge a journal line.

    This is the ONE place a remote's own words reach a message, and it exists
    for one reason (plan 3.5 / RR2-14): Codex rejected the login server-side
    for six days and the feeder said only "JSON-RPC error -32603".

    Implements: SR-069, LLR-1052
    """
    text = "" if text is None else str(text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text


# What makes a Codex error frame an AUTHENTICATION failure: the vendor's own
# `token_expired` word, or a 401 stated as such. Deliberately narrow -- a bare
# "401" in a message (a byte count, a port) is not a login.
_CODEX_AUTH = re.compile(r"(?i)token_expired|\b401\s+unauthorized\b|\"status\"\s*:\s*401\b")
# ... and what makes one THROTTLING (W11): the upstream's own 429, stated as such.
# As narrow as the auth rule and for the same reason -- `account/rateLimits/read`
# is literally about rate limits, so the bare words "rate limit" prove nothing.
_CODEX_THROTTLE = re.compile(r"(?i)\b429\s+too\s+many\s+requests\b|\"status\"\s*:\s*429\b")


def codex_error_frame(frame):
    """Turn a JSON-RPC error frame into the right SourceFailure.

    The SERVER'S MESSAGE is included -- truncated and redacted (see
    `redact_remote_text`) -- and the failure is an `AuthFailure(code=
    "token_expired")` when the frame says the login is dead. `data`, when the
    server sends one, is read for CLASSIFICATION and for the message alike,
    because a `token_expired` can arrive in either.
    """
    error = frame.get("error")
    if not isinstance(error, dict):
        return SourceFailure("codex: JSON-RPC error (no detail)")
    code = error.get("code")
    code_text = str(code) if isinstance(code, int) and not isinstance(code, bool) else "?"
    parts = []
    for field in ("message", "data"):
        value = error.get(field)
        if value is None:
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
    raw = " ".join(parts)
    text = "codex: JSON-RPC error %s" % code_text
    detail = redact_remote_text(raw)
    if detail:
        text += ": " + detail
    if _CODEX_AUTH.search(raw):
        return AuthFailure(text, code="token_expired")
    if _CODEX_THROTTLE.search(raw):
        return Throttled(text, status=429)
    return SourceFailure(text)


def end_child(child, reap_seconds=5):
    """Kill `child` AND everything it started, then reap it. Never raises.

    THE ONESHOT MUST NEVER LEAVE A CHILD RUNNING (W11 task 3; the class of
    fault W1 fixed). `read_codex` starts the app-server as a session leader, so
    its process-group id is its pid and one `killpg` reaches every descendant
    that did not deliberately leave the group. Where there is no process group
    to kill (a test double, a non-POSIX host) the child alone is killed, as
    before. The wait is bounded: a child that will not die after SIGKILL is the
    kernel's problem, not a reason to hang the cycle.

    Implements: SR-095, LLR-1194
    """
    pid = getattr(child, "pid", None)
    killed = False
    if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            # Only a group the child LEADS: never this feeder's own group.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
                killed = True
        except OSError:
            pass
    if not killed:
        try:
            child.kill()
        except OSError:
            pass
    wait = getattr(child, "wait", None)
    if wait is not None:
        try:
            wait(timeout=reap_seconds)
        except Exception:
            pass


def read_codex(env):
    """Source 1: `codex app-server` JSON-RPC `account/rateLimits/read`.

    NO CREDENTIAL PASSES THROUGH THIS PROCESS. The child reads its own
    `~/.codex/auth.json`; we write two JSON-RPC frames to its stdin and read
    one result off its stdout. That is why codex is the source with no
    credential knob.
    """
    binary = env.get("AI_USAGE_CODEX_BIN") or "codex"
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    # The unit names the Codex home it signs in under; no auth.json there is
    # "login owed", not a launch worth making (and not an error).
    codex_home = env.get("CODEX_HOME")
    if codex_home and path_absent(os.path.join(codex_home, "auth.json")):
        raise PendingState("login owed: no codex sign-in at %s yet" % os.path.join(codex_home, "auth.json"))
    try:
        # ITS OWN PROCESS GROUP, so `end_child` can take the whole tree down.
        # The app-server is not one process: measured on the hub 2026-10-22, a
        # read that timed out had spawned over a hundred `lsb_release` shells
        # beneath it, and killing only the app-server leaves those to whoever
        # cleans up next (systemd does, at the end of the unit -- a hand run has
        # no such janitor).
        child = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            start_new_session=(os.name == "posix"))
    except FileNotFoundError as exc:
        if binary_absent(binary, env):
            raise PendingState("codex: not installed yet%s" % provisioning_hint(env, "codex"))
        raise SourceFailure("codex: cannot launch %s (%s)" % (binary, type(exc).__name__))
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
        end_child(child)

    frame = result.get("frame")
    if frame is None:
        raise SourceFailure("codex: no rateLimits answer within %ss" % timeout)
    if "error" in frame:
        raise codex_error_frame(frame)
    return parse_codex(frame.get("result"), cycle_now(env))


def _read_claude_account(env, name, credentials_knob, buckets, scoped_models):
    """One Claude account's usage, read with that account's own credential file.

    THE ONLY PLACE "carries no usable key" BECOMES AUTHENTICATION. `NoUsableKey`
    is a plain SourceFailure everywhere else (OpenCode's renamed schema produced
    the same words from a good credential), but a Claude credentials file that
    parses and holds no access token is what a lapsed or half-rewritten login
    looks like. Confirming it over attempts spanning ten minutes (`run_cycle`,
    `auth_failure_confirmed`) is what absorbs the refresh unit rewriting the
    file mid-read.
    """
    path = env.get(credentials_knob)
    if not path:
        raise PendingState("%s: login owed (%s is not set)" % (name, credentials_knob))
    try:
        token = read_secret_file(path, _claude_token)
    except NoUsableKey as exc:
        raise AuthFailure(str(exc), code="no_usable_key")
    version = env.get("AI_USAGE_CLAUDE_VERSION") or "2.0.0"
    url = env.get("AI_USAGE_CLAUDE_URL") or "https://api.anthropic.com/api/oauth/usage"
    body = http_json(url, {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/" + version,
        "Accept": "application/json",
    }, int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30))
    return parse_claude(body, cycle_now(env), buckets, scoped_models, name)


def read_claude(env):
    """Source 2: the OAuth usage endpoint, with its beta header and UA."""
    return _read_claude_account(env, "claude", "AI_USAGE_CLAUDE_CREDENTIALS",
                                CLAUDE_BUCKETS, CLAUDE_SCOPED_MODELS)


def read_claude2(env):
    """Source 2b: the SAME endpoint for the second account.

    Its credential is a different file, written by the CLI run with its own
    `CLAUDE_CONFIG_DIR` (`homehub-claude-refresh@claude2` keeps it alive). The
    two accounts never share a file, and neither unit ever touches the other's:
    a refresh token is single-use and rotating, so a copy refreshed anywhere
    else would retire the live one.
    """
    return _read_claude_account(env, "claude2", "AI_USAGE_CLAUDE2_CREDENTIALS",
                                CLAUDE2_BUCKETS, CLAUDE2_SCOPED_MODELS)


def _gemini_token(data):
    """(access_token, expiry_date in ms or None) from the CLI's oauth_creds.json.

    The file is google-auth-library's `Credentials` as `oauth2.ts`
    `cacheCredentials` writes it: `access_token`, `refresh_token`, `expiry_date`
    (epoch MILLISECONDS), `scope`, `token_type`, `id_token`. Only the first and
    the third are read -- never the refresh token. This feeder does not refresh
    a Google token and so has no use for one.
    """
    if not isinstance(data, dict):
        return None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expiry = data.get("expiry_date")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) \
            or not math.isfinite(expiry):
        expiry = None
    return token, expiry


def read_gemini(env):
    """Source 4: Gemini CLI's quota call, using the token the CLI itself keeps.

    THE FEEDER NEVER MINTS OR REFRESHES A GOOGLE TOKEN (SR-021). It reads
    `access_token` out of the file Gemini CLI maintains and spends it on two
    calls the CLI makes itself. If the token has expired it says so and stops --
    renewing it is `homehub-gemini-refresh`'s job, running the CLI, in a
    process that is allowed to write that file. A token expired by its own
    `expiry_date` is an AuthFailure: left alone it means the CLI's refresh is
    not happening, which is a dead login as far as the wall is concerned, and
    the ten-minute confirmation absorbs the moments between expiry and renewal.

    It also never ONBOARDS: no `currentTier` in the `loadCodeAssist` answer is a
    refusal (see `parse_gemini_project`), not a trigger for `onboardUser`.

    Implements: SR-021, SR-067, LLR-1050
    """
    path = env.get("AI_USAGE_GEMINI_CREDENTIALS")
    if not path:
        raise PendingState("gemini: login owed (AI_USAGE_GEMINI_CREDENTIALS is not set)")
    models = parse_gemini_models(env.get("AI_USAGE_GEMINI_MODELS"))
    now = cycle_now(env)
    token, expiry_ms = read_secret_file(path, _gemini_token)
    if expiry_ms is not None and expiry_ms / 1000.0 <= now:
        raise AuthFailure(
            "gemini: the CLI's access token has expired and has not been renewed "
            "(homehub-gemini-refresh keeps it alive)", code="access_token_expired")
    base = (env.get("AI_USAGE_GEMINI_URL") or GEMINI_DEFAULT_BASE_URL).rstrip("/")
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": env.get("AI_USAGE_AGENT") or "homehub-ai-usage/1",
        "Accept": "application/json",
    }
    configured = (env.get("AI_USAGE_GEMINI_PROJECT") or "").strip() or None
    metadata = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI"}
    load_request = {"metadata": metadata}
    if configured and _GEMINI_PROJECT_ID.match(configured):
        load_request["cloudaicompanionProject"] = configured
        metadata["duetProject"] = configured
    project = parse_gemini_project(
        http_json(base + ":loadCodeAssist", headers, timeout, data=load_request),
        configured)
    quota = http_json(base + ":retrieveUserQuota", headers, timeout,
                      data={"project": project})
    return parse_gemini_quota(quota, now, models)


def read_opencode(env):
    """Source 3: the Zen usage endpoint, with the workspace bearer key."""
    path = env.get("AI_USAGE_OPENCODE_AUTH")
    if not path:
        raise PendingState("opencode: login owed (AI_USAGE_OPENCODE_AUTH is not set)")
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
    "claude2": read_claude2,
    "gemini": read_gemini,
    "opencode": read_opencode,
}


def enabled_sources(env):
    """Which sources to read. Absent knob = the original three; each is optional.

    `claude2` and `gemini` are OPT-IN: they need a second sign-in and a second
    credential file, so an absent knob must not start posting a gauge for a
    login nobody has made.

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
    """One feeder cycle: read every DUE source, post the gauges of what was read.

    Contract:
      Inputs:  env: the process environment (plus `_identity`); `readers` and
               `poster` are injectable so the tests can exercise every failure
               class without the network.
      Outputs: (posted: list of (key, fresh, ok), failures: list of str).
               Skipped sources are named in env["_notes"], not in either list.

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

    EACH SOURCE HAS ITS OWN SCHEDULE (W11). The timer runs every minute; a
    source that is not due, or is backing off after a 429/5xx, is SKIPPED: it is
    not read and its gauges are not posted, so NagLight keeps the body it was
    last sent -- the true reading at its true `observed_at`, or the unavailable
    body if that is what the source last earned. A skip is its own outcome: it
    neither resets nor advances the source's backoff or authentication history.

    A SOURCE THAT FAILS AUTHENTICATION IS CONFIRMED OVER ATTEMPTS AND TIME
    (plan 3.6 G2, revised by W11): `AuthFailure` is caught before
    `SourceFailure`; the failed attempts ride in the schedule entry; and once
    `auth_failure_confirmed` holds, each stored reading of that source is
    replaced by an auth marker, key kept, so a later failure of any other kind
    cannot restore the old colour and an optional gauge still posts. A success
    resets the history; any other failure leaves it as it is.

    Implements: SR-021, SR-068, SR-095, LLR-005, LLR-1053, LLR-1054, LLR-1055,
                LLR-1190, LLR-1191, LLR-1192, LLR-1193
    """
    now = int(now if now is not None else time.time())
    readers = readers or SOURCE_READERS
    state_path = env.get("AI_USAGE_STATE_FILE") or "/var/lib/homehub-ai/usage-state.json"
    state_root = resolve_state_root(env)
    feed_url = env["_feed_url"]
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    active = enabled_sources(env)
    intervals = source_intervals(env, active)
    # One clock for the whole cycle: the parsers check each vendor window
    # against the same `now` the gauge will be stamped with (see `cycle_now`).
    env["_now"] = now

    # The state comes FIRST now: the schedule in it decides which sources are
    # read at all.
    state = load_state(state_path, now)
    # Presence, not content -- see state_keys. A corrupt stored entry must not
    # look like a gauge we have never seen.
    ever_seen = state_keys(state_path)
    specs = gauge_specs(env)
    previous_schedule = state.get(SCHEDULE_KEY) or {}
    # Only enabled sources keep an entry: a source switched off forgets its
    # schedule and its authentication history, as it used to forget its count.
    schedule = dict((n, previous_schedule[n]) for n in active if n in previous_schedule)

    readings, failures = {}, []
    attempted, skipped = set(), set()
    pending = set()        # sources in a quiet expected state: not installed yet / login owed
    for name in active:
        entry = schedule.get(name)
        wait = schedule_wait(entry, now)
        if wait > 0:
            skipped.add(name)
            env.setdefault("_notes", []).append(skip_note(name, entry, wait, intervals[name]))
            continue
        attempted.add(name)
        outcome, retry_after = "ok", None
        sink = []
        token = _RATE_LIMIT_SINK.set(sink)
        try:
            readings.update(readers[name](env))
        # AuthFailure and Throttled BEFORE SourceFailure, and the order is the
        # feature: both are subclasses of it, so listed after it they would be
        # swallowed by the plain handler and a dead login (plan 3.6, G2) or a
        # vendor asking us to slow down (W11) would look like a network blip.
        except PendingState as exc:
            # Quiet and expected: one INFO line (main prints the notes), the gauges
            # go down the unavailable path, and it is NOT a failure.
            outcome = "pending"
            pending.add(name)
            env.setdefault("_notes", []).append("%s: %s" % (name, exc) if not str(exc).startswith(name) else str(exc))
        except AuthFailure as exc:
            outcome = "auth"
            failures.append("%s: %s" % (name, exc))
        except Throttled as exc:
            outcome, retry_after = "throttled", exc.retry_after
            failures.append("%s: %s" % (name, exc))
        except SourceFailure as exc:
            outcome = "failure"
            failures.append("%s: %s" % (name, exc))
        except Exception as exc:               # a reader bug is a source failure
            outcome = "failure"
            failures.append("%s: unexpected %s" % (name, type(exc).__name__))
        finally:
            _RATE_LIMIT_SINK.reset(token)
        headers = {}
        for seen in sink:
            headers.update(seen)
        schedule[name] = next_schedule(entry, outcome, now, intervals[name],
                                       retry_after=retry_after, ratelimit=headers)
        if outcome == "throttled":
            failures.append(backoff_line(name, schedule[name], now, retry_after))
        if outcome == "auth":
            failures.append(auth_line(name, schedule[name]))

    confirmed = set(name for name in attempted
                    if auth_failure_confirmed(schedule.get(name)))
    if schedule:
        state[SCHEDULE_KEY] = schedule
    else:
        state.pop(SCHEDULE_KEY, None)
    # MARK, NEVER DELETE: replace each stored reading with an auth marker, key
    # kept, so a later failure of any other kind cannot bring the old colour
    # back (the sticky rule) and an optional gauge still posts. A gauge this
    # box has never seen is left alone -- marking it would conjure a gauge
    # that never existed.
    for spec in specs:
        if spec.source in confirmed and (spec.key in state or spec.key in ever_seen):
            current = state.get(spec.key)
            if not (isinstance(current, dict) and current.get("auth_failed") is True):
                state[spec.key] = {"auth_failed": True, "at": now}

    posted = []
    for spec in specs:
        if spec.source not in active or spec.source in skipped:
            continue
        reading = readings.get(spec.key)
        stored = state.get(spec.key)
        # An OPTIONAL gauge the vendor has never mentioned is not posted at all
        # -- see GaugeSpec. It becomes a real gauge the first cycle that carries
        # it, and keeps being posted after that, stored reading and all.
        if spec.optional and reading is None and spec.key not in ever_seen:
            continue
        try:
            if spec.source in pending:
                # Nothing to read, and the cached value is NOT history worth showing:
                # the CLI or the login is gone, so post the explicit UNAVAILABLE body
                # (no observed_at), never an old reading that is still "fresh".
                body, fresh = build_gauge(spec, UNAVAILABLE_CONSUMED, None, None, None), False
            else:
                body, fresh = build_post(spec, reading, stored, now,
                                         auth_confirmed=spec.source in confirmed)
        except (ValueError, SourceFailure) as exc:
            failures.append("gauge %s: %s" % (spec.key, exc))
            body, fresh = build_gauge(spec, UNAVAILABLE_CONSUMED, None, None, None), False
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
    # The per-source interval knobs refuse here too, so a malformed one stops
    # the unit at its ExecStartPre instead of at the first cycle.
    source_intervals(env, enabled_sources(env))
    if "--check" in argv:
        print("ai-usage: configuration accepted (identity set, destination local).")
        return 0
    posted, failures = run_cycle(env)
    for key, fresh, ok in posted:
        print("ai-usage: %-32s %-12s %s" % (key, "live" if fresh else "UNAVAILABLE",
                                            "posted" if ok else "POST FAILED"))
    for line in failures:
        print("ai-usage: %s" % line, file=sys.stderr)
    for note in env.get("_notes", []):
        print("ai-usage: INFO %s" % note)
    # ONE line that says this invocation reached its end. ai-cli-status.sh reads
    # it (with the journal's invocation id) to tell a completed cycle from one
    # that died part-way; without it a half-printed cycle looks like evidence.
    print("ai-usage: cycle complete gauges=%d failures=%d" % (len(posted), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
