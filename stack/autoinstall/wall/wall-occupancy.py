#!/usr/bin/env python3
"""The wall panel's occupancy power decision — ONE pure core, both consumers.

WHY THIS FILE EXISTS AT ALL, and why it is not two lines of bash in
`wall-sleep.sh`. SN-015 asks for occupancy to drive the backlight *and* the
suspend/RTC path. The obvious implementation — a presence check next to the
backlight write, and a second presence check next to the suspend — is exactly
the defect this module exists to prevent: two conditions that agree on the day
they are written and drift afterwards, leaving either a dark screen on an awake
machine or a machine that suspends with somebody standing in front of it.

So there is ONE function, `decide()`, and it returns ONE record naming BOTH the
backlight state and the power action. `wall-sleep.sh` applies that record; it
never re-derives either half. If the two behaviours ever disagree it will be
because `decide()` said so, in a single place, with a single test.

PURE CORE — no I/O, no clock, no environment. `decide()` is a total function of
its arguments; the only I/O in this file is `read_presence()` (one file read)
and `main()` (argument parsing and printing). That split is what lets the whole
truth table be unit-tested on Windows with no panel, no sensors and no systemd.

THE FAIL-SAFE DIRECTION, stated once because everything below depends on it:
when the presence signal is missing, stale, malformed, or says "unknown", the
panel is treated as OCCUPIED. A wall panel that wrongly stays lit is a visible
nuisance; a wall panel that wrongly suspends is unreachable over the LAN until
the RTC alarm fires (see SN-013's S3 edge cases) and cannot be woken from its
own touchscreen. The failure directions are not symmetric, so neither is the
default. This is also why an image that ships with no presence writer at all
behaves exactly like today's schedule: absence is never *asserted*, so the
absence timer never starts.

Implements: SR-020, LLR-003
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# The three presence values this module understands, after normalisation.
# "unknown" is deliberately NOT one of them — `normalise_presence()` collapses
# every uncertain input to PRESENT so that no caller can accidentally treat
# "we don't know" as "nobody is here".
PRESENT = "present"
ABSENT = "absent"

# The presence file's schema version (IF-012). A file that does not carry
# exactly this integer is not understood, and "not understood" means PRESENT.
PRESENCE_SCHEMA_VERSION = 1

# How far in the future an observation may be stamped before we call it a
# broken clock rather than a fresh reading. The panel's RTC and system clock can
# disagree across a resume, and a reading from "the future" would otherwise stay
# fresh forever — a stuck sensor that pins the panel awake is benign, but a
# stuck sensor that pins it ASLEEP would not be, so the skew is bounded either
# way and an over-skewed reading is discarded (i.e. read as PRESENT).
MAX_CLOCK_SKEW_MS = 120_000

# The floor below which an "absence started at" epoch is not a timestamp at all.
# 1_600_000_000 is 2020-09-13, years before this image existed, so no honest
# reading can be under it. The value this exists to reject is a TRUNCATED one:
# a `1` left behind by an interrupted or non-atomic write reads as 1970 and
# therefore as five decades of absence, which is an IMMEDIATE suspend the first
# time the panel steps outside the on-period. The absence clock is the only
# input that can put the panel to sleep, so it is range-checked rather than
# trusted to be well-formed.
MIN_PLAUSIBLE_EPOCH = 1_600_000_000


def _reject_non_finite(token):
    """`json.loads`'s hook for the three constants JSON itself does not define.

    Contract:
      Inputs:  token: str — "NaN", "Infinity" or "-Infinity"
      Outputs: never returns
      Raises:  ValueError, always

    Python's `json.loads` accepts all three by default, and a NaN is the one
    value that defeats EVERY freshness check at once: every comparison against
    NaN is false, so `age_ms > ttl_ms` is false, `age_ms < -skew` is false, and
    a reading with `"observedAt": NaN` sails through as fresh. A file that then
    says "absent" would be believed, which inverts this module's whole fail-safe
    direction. So the parse fails and the caller reads PRESENT.

    Implements: SR-020, LLR-003
    """
    raise ValueError("non-finite JSON constant: {}".format(token))


def _finite_number(value):
    """Is this a real, finite number — not a bool, not NaN, not an infinity?

    Contract:
      Inputs:  value: anything
      Outputs: bool

    `_reject_non_finite` stops the literal `NaN`/`Infinity` tokens; this stops
    the other door into the same failure, an in-range literal that OVERFLOWS to
    infinity on parse (`1e999` is a perfectly legal JSON number and becomes
    `inf`). Both roads end at a comparison that cannot be false.

    Implements: SR-020, LLR-003
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def parse_absent_since(text, now_epoch):
    """Turn the absence clock's file contents into a trustworthy epoch, or None.

    Contract:
      Inputs:  text: str — the raw contents of the absence clock file
               now_epoch: int — seconds
      Outputs: (absent_since, reason) — absent_since is int|None, and None
               means "no usable absence start", which restarts the timer.
      Raises:  nothing.

    None is the SAFE answer: `decide()` treats it as an absence that has not yet
    lasted any measurable time, so the panel stays awake for a full timeout.
    That is why every doubtful reading is turned into None rather than into a
    best guess.

    Implements: SR-020, LLR-003
    """
    raw = str(text).strip()
    if not raw:
        return None, "no absence start recorded"
    if not raw.isdigit():
        return None, "absence clock is not an epoch ({!r}) — restarting it".format(raw[:32])
    value = int(raw)
    if value < MIN_PLAUSIBLE_EPOCH:
        return None, (
            "absence clock reads {} — too small to be a timestamp (a truncated "
            "write); restarting it rather than believing decades of absence".format(value)
        )
    if value > now_epoch + MAX_CLOCK_SKEW_MS // 1000:
        return None, "absence clock is in the future ({}) — restarting it".format(value)
    return value, ""


# ── times ────────────────────────────────────────────────────────────────────


def parse_hhmm(value):
    """Parse a 24h `HH:MM` wall-clock time into minutes past local midnight.

    Contract:
      Inputs:  value: str, `HH:MM` with 0 <= HH <= 23 and 0 <= MM <= 59
      Outputs: int in [0, 1440)
      Raises:  ValueError, with the offending text, on anything else

    It raises rather than defaulting on purpose: a typo'd boundary silently
    coerced to midnight is a panel that sleeps at the wrong hour and reports
    nothing, which is the failure mode wall-firstboot.sh's timezone warning
    already exists to prevent.

    Implements: LLR-003
    """
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("not a 24h HH:MM time: {!r}".format(value))
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError("not a 24h HH:MM time: {!r}".format(value))
    return hours * 60 + minutes


def in_on_period(minute_of_day, on_start, on_end):
    """Is this minute inside the panel's on-period?

    Contract:
      Inputs:  minute_of_day: int in [0, 1440)
               on_start: int — SLEEP_END, the morning boundary (06:45 = 405)
               on_end:   int — SLEEP_START, the evening boundary (22:00 = 1320)
      Outputs: bool

    The on-period is `[on_start, on_end)` — half-open, so the instant the
    evening boundary arrives the panel is already outside it and the absence
    timer can begin to count. A window that WRAPS midnight (on_start > on_end)
    is supported because nothing forbids configuring one.

    Equal boundaries mean "on all day": that is the fail-safe reading of a
    degenerate configuration, since the alternative — "off all day" — would let
    a single mistyped knob suspend the panel around the clock.

    Implements: LLR-003
    """
    if on_start == on_end:
        return True
    if on_start < on_end:
        return on_start <= minute_of_day < on_end
    return minute_of_day >= on_start or minute_of_day < on_end


# ── presence ─────────────────────────────────────────────────────────────────


def normalise_presence(state):
    """Collapse any presence word to PRESENT or ABSENT, erring toward PRESENT.

    Contract:
      Inputs:  state: anything. Only the exact string "absent" means absent.
      Outputs: PRESENT | ABSENT

    Implements: LLR-003
    """
    return ABSENT if state == ABSENT else PRESENT


def read_presence(path, now_ms):
    """Read the IF-012 presence signal file, failing safe to PRESENT.

    Contract:
      Inputs:  path: str — the presence file (WALL_PRESENCE_FILE)
               now_ms: int — current time, epoch milliseconds
      Outputs: (state, reason) where state is PRESENT|ABSENT and reason is a
               short human string for the journal — always populated, so a
               panel that is staying awake can always say why.
      Raises:  nothing. Every failure is a PRESENT with a reason.

    ABSENT is returned only when a well-formed, fresh, correctly versioned file
    explicitly says so. Everything else — missing file, unreadable file, bad
    JSON, wrong schemaVersion, missing fields, an observation older than its own
    ttlMs, or a timestamp implausibly far in the future — is PRESENT.

    Implements: SR-020, LLR-003
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return PRESENT, "no presence file at {} (no absence is being asserted)".format(path)
    except OSError as error:
        return PRESENT, "presence file unreadable ({})".format(error)

    try:
        payload = json.loads(raw, parse_constant=_reject_non_finite)
    except ValueError:
        # Includes the NaN/Infinity rejection above: a presence file carrying a
        # non-finite number is not a fresh reading, it is a broken one.
        return PRESENT, "presence file is not valid JSON"
    if not isinstance(payload, dict):
        return PRESENT, "presence file is not a JSON object"
    if payload.get("schemaVersion") != PRESENCE_SCHEMA_VERSION:
        return PRESENT, "presence file schemaVersion is not {}".format(PRESENCE_SCHEMA_VERSION)

    observed_at, ttl_ms = payload.get("observedAt"), payload.get("ttlMs")
    if not _finite_number(observed_at):
        return PRESENT, "presence file has no finite numeric observedAt"
    if not _finite_number(ttl_ms) or ttl_ms <= 0:
        return PRESENT, "presence file has no positive finite ttlMs"
    age_ms = now_ms - observed_at
    if age_ms < -MAX_CLOCK_SKEW_MS:
        return PRESENT, "presence reading is stamped in the future (clock skew)"
    if age_ms > ttl_ms:
        return PRESENT, "presence reading is stale ({} ms old, ttl {} ms)".format(
            int(age_ms), int(ttl_ms)
        )

    state = payload.get("presence")
    if state == ABSENT:
        return ABSENT, "presence file reports absent (source {})".format(payload.get("source"))
    if state == PRESENT:
        return PRESENT, "presence file reports present (source {})".format(payload.get("source"))
    return PRESENT, "presence file reports {!r}, which is not present/absent".format(state)


# ── the one decision ─────────────────────────────────────────────────────────


def decide(
    now_epoch,
    minute_of_day,
    presence,
    absent_since,
    absence_enabled,
    absence_timeout_min,
    on_start,
    on_end,
):
    """The single source of truth for BOTH the backlight and the power action.

    Contract:
      Inputs:  now_epoch: int — seconds; only used against absent_since
               minute_of_day: int in [0, 1440) — local wall-clock minute
               presence: PRESENT | ABSENT (already normalised/fail-safed)
               absent_since: int|None — epoch seconds the current absence began,
                             None when present or when nothing has been recorded
               absence_enabled: bool — WALL_ABSENCE_ENABLED
               absence_timeout_min: int >= 0 — WALL_ABSENCE_TIMEOUT_MIN
               on_start: int — minutes; the on-period start == SLEEP_END == the
                         RTC wake time. ONE value, three jobs, by SN-015.
               on_end:   int — minutes; the on-period end == SLEEP_START
      Outputs: dict {
                 "backlight":     "on" | "off" | "unchanged",
                 "power":         "stay" | "suspend",
                 "on_period":     bool,
                 "absence_clock": "start" | "restart" | "clear" | "untouched",
                 "reason":        str,
               }
      Raises:  nothing.

    The three states SN-015 distinguishes, and nothing else:

      1. absence detection DISABLED -> {"unchanged", "stay"}. Occupancy touches
         neither half; the existing SLEEP_START=22:00 / SLEEP_END schedule runs
         exactly as it does today. This branch is first precisely so that no
         later line can accidentally act on a panel that opted out.
      2. absence detection ENABLED, PRESENT -> backlight on, never suspend.
         Inside the on-period this is the walk-in case: because nothing ever
         suspended, lighting the screen is a backlight write and NOT a resume.
      3. absence detection ENABLED, ABSENT -> backlight off, and then the only
         place a suspend can be decided: outside the on-period, once the
         absence has lasted absence_timeout_min. Inside the on-period the
         suspend is unreachable by construction, which is what makes "presence
         inside the on-period never suspends" a property of this function
         rather than an ordering accident in the caller.

    THE HOUR IS AN HOUR SPENT OUTSIDE THE ON-PERIOD, and this record says so by
    also naming what to do with the absence clock. The rule the block exists to
    enforce is "absence_timeout_min of absence OUTSIDE the on-period", so an
    absence that BEGAN inside it must not carry its elapsed time across the
    boundary: somebody who left at 12:00 and is still away at 22:00 would
    otherwise present the decider with 600 minutes the moment the on-period
    closed, and the panel would suspend on the very first tick outside it, with
    the required hour outside never observed at all. So the clock is CLEARED
    for every minute that is present or inside the on-period, and only STARTED
    once absence and "outside" hold together. The caller owns the file; this
    function decides what happens to it, as it decides everything else here:
    "clear" removes it, "start" leaves a usable clock running, and "restart"
    replaces whatever is there with now — which is what a MISSING clock and an
    UNUSABLE one (a truncated write, a future stamp) both need, and the reason
    they are one word rather than two.

    An ABSENT with no recorded absent_since cannot have lasted an hour, so it
    stays awake — the recording is on tmpfs and a reboot therefore restarts the
    clock, which is exactly the behaviour SN-013's mains-blip case wants.

    Implements: SR-020, LLR-003
    """
    on_period = in_on_period(minute_of_day, on_start, on_end)

    if not absence_enabled:
        return {
            "backlight": "unchanged",
            "power": "stay",
            "on_period": on_period,
            "absence_clock": "untouched",
            "reason": "absence detection disabled — the SLEEP_START schedule is unchanged",
        }

    if presence == PRESENT:
        return {
            "backlight": "on",
            "power": "stay",
            "on_period": on_period,
            "absence_clock": "clear",
            "reason": "present — the panel never suspends while somebody is here",
        }

    if on_period:
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": True,
            "absence_clock": "clear",
            "reason": "absent inside the on-period — backlight off only, so a walk-in "
            "needs no resume; the absence clock does NOT run in here, so the hour a "
            "suspend needs is an hour spent OUTSIDE the on-period",
        }

    if absent_since is None or absent_since > now_epoch:
        # The second half of that test is the belt to parse_absent_since's
        # braces: a clock stamped in the future yields a NEGATIVE age, which is
        # not a suspend today but is nonsense arriving at a decision that can
        # suspend, so it restarts the timer instead.
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": False,
            "absence_clock": "restart",
            "reason": "absent outside the on-period, but no usable absence start is "
            "recorded — the timer starts now",
        }

    absent_minutes = (now_epoch - absent_since) / 60.0
    if absent_minutes < absence_timeout_min:
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": False,
            "absence_clock": "start",
            "reason": "absent {:.0f} of {} min outside the on-period".format(
                absent_minutes, absence_timeout_min
            ),
        }

    return {
        "backlight": "off",
        "power": "suspend",
        "on_period": False,
        "absence_clock": "start",
        "reason": "absent {:.0f} min (>= {}) outside the on-period — suspend, RTC wake at "
        "the on-period start".format(absent_minutes, absence_timeout_min),
    }


# ── thin shell ───────────────────────────────────────────────────────────────


def _bool(text):
    return str(text).strip().lower() == "true"


def main(argv=None):
    """Print one decision as `KEY=VALUE` lines for `wall-sleep.sh` to apply.

    Deliberately NOT JSON: the consumer is bash, and a shell that has to parse
    JSON either grows a python dependency in its hot path or grows a `sed` that
    is wrong on the first value containing a brace. `KEY=VALUE` with the reason
    last and unquoted is read with a single `while IFS='=' read`.

    Exit codes: 0 always on a decision (the decision itself carries the
    outcome); 2 on unusable arguments — a wrong boundary must be loud, never a
    silently defaulted midnight.

    Implements: LLR-003
    """
    parser = argparse.ArgumentParser(description="Decide the wall panel's occupancy power state.")
    parser.add_argument("--now-epoch", type=int, required=True)
    parser.add_argument("--minute-of-day", type=int, required=True)
    parser.add_argument("--presence-file", required=True)
    parser.add_argument("--absent-since", default="")
    parser.add_argument("--absence-enabled", default="false")
    parser.add_argument("--absence-timeout-min", type=int, default=60)
    parser.add_argument("--on-start", required=True, help="SLEEP_END, HH:MM")
    parser.add_argument("--on-end", required=True, help="SLEEP_START, HH:MM")
    args = parser.parse_args(argv)

    try:
        on_start = parse_hhmm(args.on_start)
        on_end = parse_hhmm(args.on_end)
    except ValueError as error:
        print("wall-occupancy: {}".format(error), file=sys.stderr)
        return 2
    if args.absence_timeout_min < 0:
        print("wall-occupancy: --absence-timeout-min must be >= 0", file=sys.stderr)
        return 2

    presence, presence_reason = read_presence(args.presence_file, args.now_epoch * 1000)
    absent_since, clock_reason = parse_absent_since(args.absent_since, args.now_epoch)

    result = decide(
        now_epoch=args.now_epoch,
        minute_of_day=args.minute_of_day,
        presence=presence,
        absent_since=absent_since,
        absence_enabled=_bool(args.absence_enabled),
        absence_timeout_min=args.absence_timeout_min,
        on_start=on_start,
        on_end=on_end,
    )

    print("PRESENCE={}".format(presence))
    print("BACKLIGHT={}".format(result["backlight"]))
    print("POWER={}".format(result["power"]))
    print("ON_PERIOD={}".format("true" if result["on_period"] else "false"))
    # What the caller must do with the absence clock. It travels WITH the
    # decision for the same reason the backlight and the power action do: the
    # clock is the one input that makes a suspend reachable, so a shell that
    # managed it on its own rules would be a second decider.
    print("ABSENCE_CLOCK={}".format(result["absence_clock"]))
    # The RTC wake time and the on-period start are THE SAME KNOB (SLEEP_END).
    # It is echoed here so the applying shell arms the alarm from the decision
    # it is applying, rather than re-reading the knob and possibly a different
    # one. Same reason the backlight and the power action travel together.
    print("RTC_WAKE={}".format(args.on_start))
    print("PRESENCE_REASON={}".format(presence_reason))
    print("REASON={}".format(result["reason"]))
    if clock_reason:
        print("CLOCK_REASON={}".format(clock_reason))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    sys.exit(main())
