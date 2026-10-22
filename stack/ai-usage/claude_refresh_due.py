#!/usr/bin/env python3
"""Is the Claude access token close enough to expiry for the refresher to run?

ONE RESPONSIBILITY: the ExecCondition gate of homehub-claude-refresh@.service.
It reads the `expiresAt` of the credential the CLI keeps, says how long the
access token has left, and exits:

  0  run the refresher: the token expires within LEAD_SECONDS, has expired,
     or nothing trustworthy could be read (no file, no expiry, a value that
     cannot be an expiry) -- every doubt resolves to RUNNING the CLI, which is
     what the unit did unconditionally before this gate existed;
  3  not due: the token is valid for longer than LEAD_SECONDS.

The unit maps 3 to a skip and EVERY other code -- including a crash, which
Python reports as 1, and a missing file, 2 -- to a run, so this gate can only
ever add runs, never take the last one away.

WHY IT EXISTS (W11, measured on the hub 2026-10-12..21). The refresher used to
run `claude -p "/usage"` every 4 h against an 8 h token, on the assumption that
the CLI renews whenever it runs. It does not: every Claude 401 the feeder logged
in that window (14 of them) came 237-242 min after the previous refresher run
and cleared at the next one, i.e. the token lapsed and stayed lapsed until the
refresher happened to come round. On a 10-minute feeder that was a rare grey
sample; on a 1-minute feeder that confirms authentication failure over 10
minutes it would grey the Claude ring whenever a lapse outlasted ten minutes
(two consecutive 401 samples, 11 min apart, on 2026-10-12). Checking every 4 min
and running the CLI only inside the last LEAD_SECONDS keeps the token renewed
before it lapses without running the CLI 360 times a day.

IT NEVER WRITES, AND NEVER PRINTS THE TOKEN. It reads one number out of the
file, and prints at most one line, with minutes, never a credential value.

Contract:
  Inputs:  argv[1] (optional): the credential path. Default
           ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json -- the file the
           CLI run by this unit instance reads, so @claude2 (which sets
           CLAUDE_CONFIG_DIR in its drop-in) is gated on its own token.
  Outputs: exit status as above; one line on stdout when it says "run".

Implements: SR-096, LLR-1195
"""

import json
import math
import os
import sys
import time

# Run the CLI once the token has less than this left. Five checks (the timer's
# 4 minutes) fit inside it, so a run that lands while the vendor is briefly
# unreachable still leaves four more before the token lapses.
LEAD_SECONDS = 20 * 60
# An expiry further out than this is not one an 8-hour token can have; it is
# a misread (seconds taken for milliseconds, a corrupt file), and a misread
# must not be what stops the refresher.
MAX_PLAUSIBLE_SECONDS = 24 * 3600
NOT_DUE = 3


def credential_path(env):
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or "~", ".claude")
    return os.path.join(os.path.expanduser(base), ".credentials.json")


def find_expiry(data):
    """`expiresAt` from the credential JSON, as epoch SECONDS, or None.

    The CLI writes it in milliseconds under `claudeAiOauth`; a value that is
    plainly in seconds is accepted as such. Searched the way the feeder's
    `_claude_token` searches for the access token, so a renamed wrapper key does
    not silently disable the gate (it would turn it into "always run").
    """
    if not isinstance(data, dict):
        return None
    # The block the CLI's own OAuth login writes, first; anything else only if
    # it is absent.
    if isinstance(data.get("claudeAiOauth"), dict):
        data = data["claudeAiOauth"]
    for key, value in data.items():
        if key.lower() in ("expiresat", "expires_at"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                return None
            return value / 1000.0 if value > 1e11 else float(value)
        found = find_expiry(value) if isinstance(value, dict) else None
        if found is not None:
            return found
    return None


def decide(expiry, now):
    """(exit status, message) for an expiry in epoch seconds (or None). Pure."""
    if expiry is None:
        return 0, "claude-refresh: no readable expiry - running the CLI"
    left = expiry - now
    if left > MAX_PLAUSIBLE_SECONDS:
        return 0, "claude-refresh: expiry is implausibly far out - running the CLI"
    if left <= LEAD_SECONDS:
        if left <= 0:
            return 0, "claude-refresh: the access token expired %d min ago - renewing" % (-left // 60)
        return 0, "claude-refresh: %d min left on the access token - renewing" % (left // 60)
    return NOT_DUE, "claude-refresh: not due (%d min left on the access token)" % (left // 60)


def main(argv=None, env=None, now=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    path = argv[0] if argv else credential_path(env)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    status, message = decide(find_expiry(data), time.time() if now is None else now)
    # Quiet when not due: that is every 4 minutes for hours, and systemd already
    # journals the skip. A run is worth a line saying why it happened.
    if status != NOT_DUE:
        print(message)
    return status


if __name__ == "__main__":
    sys.exit(main())
