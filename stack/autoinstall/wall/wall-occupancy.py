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
        payload = json.loads(raw)
    except ValueError:
        return PRESENT, "presence file is not valid JSON"
    if not isinstance(payload, dict):
        return PRESENT, "presence file is not a JSON object"
    if payload.get("schemaVersion") != PRESENCE_SCHEMA_VERSION:
        return PRESENT, "presence file schemaVersion is not {}".format(PRESENCE_SCHEMA_VERSION)

    observed_at, ttl_ms = payload.get("observedAt"), payload.get("ttlMs")
    if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool):
        return PRESENT, "presence file has no numeric observedAt"
    if not isinstance(ttl_ms, (int, float)) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        return PRESENT, "presence file has no positive ttlMs"
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
                 "backlight": "on" | "off" | "unchanged",
                 "power":     "stay" | "suspend",
                 "on_period": bool,
                 "reason":    str,
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
            "reason": "absence detection disabled — the SLEEP_START schedule is unchanged",
        }

    if presence == PRESENT:
        return {
            "backlight": "on",
            "power": "stay",
            "on_period": on_period,
            "reason": "present — the panel never suspends while somebody is here",
        }

    if on_period:
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": True,
            "reason": "absent inside the on-period — backlight off only, so a walk-in "
            "needs no resume",
        }

    if absent_since is None:
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": False,
            "reason": "absent outside the on-period, but no absence start is recorded — "
            "the timer starts now",
        }

    absent_minutes = (now_epoch - absent_since) / 60.0
    if absent_minutes < absence_timeout_min:
        return {
            "backlight": "off",
            "power": "stay",
            "on_period": False,
            "reason": "absent {:.0f} of {} min outside the on-period".format(
                absent_minutes, absence_timeout_min
            ),
        }

    return {
        "backlight": "off",
        "power": "suspend",
        "on_period": False,
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
    absent_since = int(args.absent_since) if str(args.absent_since).strip().isdigit() else None

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
    # The RTC wake time and the on-period start are THE SAME KNOB (SLEEP_END).
    # It is echoed here so the applying shell arms the alarm from the decision
    # it is applying, rather than re-reading the knob and possibly a different
    # one. Same reason the backlight and the power action travel together.
    print("RTC_WAKE={}".format(args.on_start))
    print("PRESENCE_REASON={}".format(presence_reason))
    print("REASON={}".format(result["reason"]))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    sys.exit(main())
