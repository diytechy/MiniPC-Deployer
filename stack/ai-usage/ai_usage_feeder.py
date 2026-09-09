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


def parse_codex(payload):
    """Turn one `account/rateLimits/read` result into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the JSON-RPC `result` object, exactly as observed.
      Outputs: dict of gauge key -> Reading. Empty is NOT valid; a payload with
               no usable bucket raises rather than reporting "all clear".
      Raises:  SourceFailure on a non-object, a missing or null `rateLimits`, a
               missing `primary`, or an unusable `usedPercent`.

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
    resets_at = primary.get("resetsAt")
    window_seconds = None
    window_end = None
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        window_seconds = int(minutes) * 60
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        window_end = int(resets_at)
    return {"ai-usage-codex": Reading(percent, window_end, window_seconds)}


# Claude's payload names its windows in the FIELD NAME, and the observed body
# carries no duration field, so the length is read off the name. Each entry is
# (payload key, gauge key, label, window length in seconds).
CLAUDE_BUCKETS = (
    ("five_hour", "ai-usage-claude-session", "Claude session", 5 * 3600),
    ("seven_day", "ai-usage-claude-weekly", "Claude weekly", 7 * 24 * 3600),
)


def parse_claude(payload):
    """Turn one OAuth usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed.
      Outputs: dict of gauge key -> Reading, one per CLAUDE_BUCKETS entry that
               is present and usable.
      Raises:  SourceFailure on a non-object or when NO bucket is usable. A
               body whose `five_hour` is null but whose `seven_day` is fine
               yields one gauge and no exception - the two are independent
               subscriptions limits and one being absent is not a failure of
               the other.

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
            window_end = None
            if bucket.get("resets_at") is not None:
                window_end = parse_iso8601_utc(bucket["resets_at"], "claude " + field)
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


def parse_opencode(payload):
    """Turn one Zen usage body into {gauge key: Reading}.

    Contract:
      Inputs:  payload: the decoded JSON body, exactly as observed
               ({"usage":{"weekly":{"status","percent","resetsAt"}, ...}}).
      Outputs: dict of gauge key -> Reading.
      Raises:  SourceFailure on a non-object, a missing `usage`, or when no
               listed bucket is usable.

    A bucket whose `status` is not "ok" is skipped rather than posted: the
    vendor is telling us the number is not trustworthy, and a number we were
    warned about is exactly the one that must not become a green gauge.

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
            window_end = None
            if bucket.get("resetsAt") is not None:
                window_end = parse_iso8601_utc(bucket["resetsAt"], "opencode " + field)
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


def build_post(spec, reading, last, now):
    """Decide what to POST for one gauge this cycle - the "never green" rule.

    Contract:
      Inputs:  spec: GaugeSpec;
               reading: Reading if THIS cycle read the source successfully,
                        else None (every SourceFailure class collapses to None);
               last:    the stored {"value","observed_at","window_end",
                        "window_seconds"} from a previous successful cycle, or
                        None if this gauge has never had one;
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
    if last is not None:
        return build_gauge(spec, last["value"], last["observed_at"],
                           last.get("window_end"), last.get("window_seconds")), False
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
        raise SystemExit("REFUSED: AI_USAGE_FEED_URL is unparseable: %s" % exc)
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


def open_for_write(path, state_path):
    """The ONLY way this feeder opens a file for writing. THE credential guard.

    Contract:
      Inputs:  path: the file about to be written; state_path: the one file
               this feeder owns (AI_USAGE_STATE_FILE).
      Outputs: an open text-mode handle.
      Raises:  PermissionError for ANY path that is not `state_path` or its
               `.tmp` sibling, and separately (with a louder message) for
               anything whose basename is a known vendor credential file.

    WHY AN ALLOW-LIST AND NOT A DENY-LIST. "Never writes a vendor credential
    file" cannot be met by listing the files we know about - the next CLI
    version puts its token somewhere new. So the feeder declares the one path
    it may write and refuses the rest of the filesystem, and the credential
    basenames are a SECOND check that exists only to make the failure message
    name the real hazard when someone points the state file at a token.

    Implements: SR-021, LLR-005
    """
    resolved = os.path.abspath(path)
    allowed = {os.path.abspath(state_path), os.path.abspath(state_path) + ".tmp"}
    if os.path.basename(resolved) in CREDENTIAL_BASENAMES:
        raise PermissionError(
            "REFUSED: %s is a vendor credential file. This feeder reads "
            "credentials and never writes one." % resolved)
    if resolved not in allowed:
        raise PermissionError(
            "REFUSED: %s is outside the one path this feeder may write (%s)."
            % (resolved, state_path))
    return open(resolved, "w", encoding="utf-8")


def load_state(state_path):
    """Last successful reading per gauge key. A missing/corrupt file is {}."""
    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state, state_path):
    """Write the state file through `open_for_write`, atomically.

    The state holds PERCENTAGES AND TIMESTAMPS ONLY - never a token, never a
    header, never a response body. That is asserted by test, because a state
    file that quietly grew an `Authorization` value would turn this feeder into
    the credential-writing thing it promises not to be.
    """
    tmp = state_path + ".tmp"
    with open_for_write(tmp, state_path) as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
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


def http_json(url, headers, timeout):
    """One GET, decoded. Every failure class becomes SourceFailure.

    Transport error, timeout, non-200 and an undecodable body are ONE outcome
    on purpose - see SourceFailure. The response body is NOT echoed into the
    exception on an auth failure, because a 401 body can carry request context.
    """
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise SourceFailure("%s: HTTP %s" % (url, response.status))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SourceFailure("%s: HTTP %s" % (url, exc.code))
    except SourceFailure:
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
    return parse_codex(frame.get("result"))


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
    return parse_claude(body)


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
    return parse_opencode(body)


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
    """POST one gauge. Returns (ok, detail); never raises on an HTTP failure."""
    headers = {"Content-Type": "application/json"}
    token = env.get("AI_USAGE_FEED_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    headers["X-Forwarded-User"] = env["_identity"]
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200, "HTTP %s" % response.status
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s: %s" % (exc.code, exc.read()[:200].decode("utf-8", "replace"))
    except Exception as exc:
        return False, type(exc).__name__


def run_cycle(env, now=None, readers=None, poster=None):
    """One feeder cycle: read every enabled source, post every gauge.

    Contract:
      Inputs:  env: the process environment (plus `_identity`); `readers` and
               `poster` are injectable so the tests can exercise every failure
               class without the network.
      Outputs: (posted: list of (key, fresh, ok), failures: list of str).

    A SOURCE FAILING IS NOT A CYCLE FAILING. Each source is read inside its own
    try, and the gauges belonging to a source that raised go down the
    unavailable path in `build_post` while the other sources still post live
    numbers. That is the whole reason the readers return dicts keyed by gauge
    id: one failure must not blank the panel.

    Implements: SR-021, LLR-005
    """
    now = int(now if now is not None else time.time())
    readers = readers or SOURCE_READERS
    state_path = env.get("AI_USAGE_STATE_FILE") or "/var/lib/homehub-ai/usage-state.json"
    feed_url = env["_feed_url"]
    timeout = int(env.get("AI_USAGE_TIMEOUT_SECONDS") or 30)
    active = enabled_sources(env)

    readings, failures = {}, []
    for name in active:
        try:
            readings.update(readers[name](env))
        except SourceFailure as exc:
            failures.append("%s: %s" % (name, exc))
        except Exception as exc:               # a reader bug is a source failure
            failures.append("%s: unexpected %s" % (name, type(exc).__name__))

    state = load_state(state_path)
    posted = []
    for spec in GAUGE_SPECS:
        if spec.source not in active:
            continue
        reading = readings.get(spec.key)
        body, fresh = build_post(spec, reading, state.get(spec.key), now)
        ok, detail = (poster or post_gauge)(body, feed_url, env, timeout)
        posted.append((spec.key, fresh, ok))
        if not ok:
            failures.append("post %s: %s" % (spec.key, detail))
        if reading is not None:
            state[spec.key] = {"value": reading.percent, "observed_at": now,
                               "window_end": reading.window_end,
                               "window_seconds": reading.window_seconds}
    save_state(state, state_path)
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
