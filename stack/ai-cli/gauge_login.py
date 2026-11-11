#!/usr/bin/env python3
"""gauge_login.py - every gauge sign-in on the hub in one table, and one command to renew each.

Run as ROOT on the hub (it reads the .env and the Google token directory, and acts as
the AI account with `runuser`). From the dev PC use RefreshGaugeLogins.cmd, which runs
this over `ssh -t`.

    gauge_login.py                  menu: the table, then offers each dead one in turn
    gauge_login.py status [--json]  the table only; always exits 0 (a diagnostic)
    gauge_login.py login <source>   one sign-in, in YOUR terminal, then verifies it
    gauge_login.py menu             same as no argument

Sources: claude, claude2, codex, gemini, opencode, google-health (alias weight),
myair and tracker-sheet (the last two have no tool-run sign-in: they print the step).

WHAT IT NEVER DOES: print a token, a key, a pasted code or any credential field. The one
field it reads out of a credential is Claude's refreshTokenExpiresAt (the hard deadline).
Whether a Claude file is blanked is decided from whether its token fields are empty,
without the values ever leaving the function that tests them.

THE VERDICT IS THE FEEDER, never an exit code: `claude auth login` prints "Login
successful." and exits 1; the usage unit exits 1 whenever ANY source is unavailable. A
source is `live` when, in the newest completed feeder run that read it (within 20 min),
at least one of its gauges is `live posted`. Optional gauges count.

Seams (tests; the defaults are the production values), mirroring ai-cli-status.sh:
  GAUGE_LOGIN_ENV_FILE     the .env                       ($HERE/../.env)
  GAUGE_LOGIN_ACCOUNT      the AI account                 (ensure-ai-account.sh --effective)
  GAUGE_LOGIN_HOME         that account's home            (getent passwd)
  GAUGE_LOGIN_RUNAS        prefix to act as the account   ("runuser -u ACCT --"); set-but-empty = none
  GAUGE_LOGIN_PYTHON       interpreter the account runs   (/usr/bin/python3)
  GAUGE_LOGIN_FEEDER_LOG / _WEIGHT_LOG / _GEMINI_LOG   files standing in for the journals
  GAUGE_LOGIN_NOW          the clock (epoch seconds)
  GAUGE_LOGIN_ASSUME_TTY   1 = treat stdin/stdout as a terminal
  GAUGE_LOGIN_VERIFY_SECONDS / _POLL_SECONDS   how long / how often verification polls (180 / 10)
  GAUGE_LOGIN_CODEX_HOME   CODEX_HOME of the usage unit   (/var/lib/homehub-ai-codex)
  GAUGE_LOGIN_MINT         the Google Health mint command (python3 ../weight/weight_oauth.py mint --force)
  GAUGE_LOGIN_SYSTEMCTL    systemctl                       (systemctl)
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
DAY = 86400
DUE_SOON_SECONDS = 3 * DAY
USAGE_EVIDENCE_MAX_AGE = 1200       # ai_usage_feeder SOURCE_INTERVAL_MAX_SECONDS (600) x2
WEIGHT_EVIDENCE_MAX_AGE = 3600      # homehub-weight.timer fires every 15 minutes
PROVIDER_IDS = ("opencode-go", "opencode")      # ai_usage_feeder _OPENCODE_PROVIDER_IDS

# --- the sources -------------------------------------------------------------------------
# kind: "usage" (homehub-ai-usage.service, gauge ids prefixed ai-usage-<name>),
#       "health" (homehub-weight.service), "manual" (no tool-run sign-in).
ORDER = ["claude", "claude2", "codex", "gemini", "opencode", "google-health", "myair", "tracker-sheet"]
ALIASES = {"weight": "google-health", "heart": "google-health", "sheet": "tracker-sheet",
           "sleep": "myair", "google": "google-health"}
LABELS = {"claude": "Claude (account 1)", "claude2": "Claude (account 2)", "codex": "Codex",
          "gemini": "Gemini", "opencode": "OpenCode", "google-health": "Google Health (weight + heart)",
          "myair": "myAir (sleep)", "tracker-sheet": "Tracker Google Sheet"}
KIND = {"claude": "usage", "claude2": "usage", "codex": "usage", "gemini": "usage",
        "opencode": "usage", "google-health": "health", "myair": "manual", "tracker-sheet": "manual"}
# What each source's gauges are called on the wall (the feeder's GAUGE_SPECS is the authority;
# tests/test_gauge_login.py compares this list to it).
GAUGES = {
    "claude": ["ai-usage-claude-session", "ai-usage-claude-weekly", "ai-usage-claude-weekly-fable"],
    "claude2": ["ai-usage-claude2-session", "ai-usage-claude2-weekly", "ai-usage-claude2-weekly-fable"],
    "codex": ["ai-usage-codex"],
    "gemini": ["ai-usage-gemini-daily"],
    "opencode": ["ai-usage-opencode-weekly", "ai-usage-opencode-monthly"],
    "google-health": ["weight", "weight-waist", "heart"],
    "myair": ["sleep"],
    "tracker-sheet": [],
}
CYCLE_END = re.compile(r"ai-usage:\s+cycle complete\b")
USAGE_LINE = re.compile(r"ai-usage:\s+(ai-usage-[a-z0-9-]+)\s+(live|UNAVAILABLE)\s+(posted|POST FAILED)")
HEALTH_LINE = re.compile(r"^(?:weight|heart):\s+([a-z-]+)\s+(live|UNAVAILABLE)\s+(posted|POST FAILED)")
# What the feeders print when a login is the reason a read failed (and ONLY then): a throttle,
# a network failure or a bad payload print UNAVAILABLE gauges with none of these.
#   usage:  ai_usage_feeder.auth_line()  "<source>: authentication failure, attempt N over Ns ..."
#           (every AuthFailure: Claude's blanked key, Codex token_expired, Gemini's expired CLI
#           token, OpenCode/vendor HTTP 401), and NoUsableKey "<source>: credential <path> carries
#           no usable key".
#   health: weight_feeder "<weight|heart>: sign-in failure N of M ..."
AUTH_USAGE = re.compile(r"ai-usage:\s+([a-z0-9]+): authentication failure\b")
# "carries no usable key" is an auth failure ONLY for Claude: the feeder promotes NoUsableKey to
# AuthFailure in _read_claude_account alone. For OpenCode the same words came from a renamed
# schema over a perfectly good key.
AUTH_NOKEY = re.compile(r"ai-usage:\s+(claude2?): credential \S+ carries no usable key")
# How a oneshot's invocation ends in the journal: the manager's own lines carry the invocation id.
HEALTH_END = re.compile(r"homehub-weight\.service: (?:Deactivated successfully|Failed with result)")
AUTH_HEALTH = re.compile(r"^(?:weight|heart):\s.*\bsign-in failure \d+ of \d+")

MANUAL_STEPS = {
    "myair": (
        "myAir has no interactive sign-in and no token: the feeder signs in with the stored\n"
        "password on every fetch. After a password change or a lockout:\n"
        "  1. On the dev PC, as the operator, update the vault:\n"
        "       PrepDeploySecrets.ps1 -ImportKey MyAirPassword -Rotate MyAirPassword\n"
        "     (the password must not contain a backslash or a double quote).\n"
        "  2. Re-materialise and deliver /opt/homehub/stack/.env through the normal lane.\n"
        "  3. As the service account, run sleep_feeder.py --retry-now to clear the back-off\n"
        "     (it refuses to run as root; the exact systemd-run line is in stack/sleep/README.md).\n"
        "  4. If the journal says myAir now asks for an emailed code, a person must sign in once;\n"
        "     see stack/sleep/README.md."),
    "tracker-sheet": None,      # needs the site domain: built in manual_step()
}


class LoginError(Exception):
    pass


# --- context ------------------------------------------------------------------------------

def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = _unquote(value)
    except OSError:
        pass
    return out


def run_quiet(cmd, timeout=30):
    """(rc, stdout). Never raises: a diagnostic must not fail the thing it reports on."""
    try:
        done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


class Ctx(object):
    def __init__(self, environ=None):
        self.environ = dict(os.environ if environ is None else environ)
        path = self.environ.get("GAUGE_LOGIN_ENV_FILE") or os.path.join(HERE, "..", ".env")
        self.env = load_env_file(path)
        self.env.update({k: v for k, v in self.environ.items() if k.startswith("GAUGE_LOGIN_")})
        self.account = self._account()
        self.home = self._home()
        if "GAUGE_LOGIN_RUNAS" in self.environ:
            self.runas = shlex.split(self.environ["GAUGE_LOGIN_RUNAS"])
        else:
            self.runas = ["runuser", "-u", self.account, "--"]
        self.python = self.environ.get("GAUGE_LOGIN_PYTHON") or "/usr/bin/python3"
        self.systemctl = shlex.split(self.environ.get("GAUGE_LOGIN_SYSTEMCTL") or "systemctl")
        self.codex_home = self.environ.get("GAUGE_LOGIN_CODEX_HOME") or "/var/lib/homehub-ai-codex"

    def _account(self):
        forced = self.environ.get("GAUGE_LOGIN_ACCOUNT")
        if forced:
            return forced
        cli_on = "true" if self.env.get("AI_CLI_ENABLED", "false") == "true" else "false"
        rc, out = run_quiet(["bash", os.path.join(HERE, "ensure-ai-account.sh"), "--effective", cli_on,
                             self.env.get("AI_CLI_USER", ""), self.env.get("AI_USAGE_USER_ACCOUNT", "")])
        return out.strip() or "homehub-ai"

    def _home(self):
        forced = self.environ.get("GAUGE_LOGIN_HOME")
        if forced:
            return forced
        rc, out = run_quiet(["getent", "passwd", self.account])
        m = re.match(r"^(?:[^:]*:){5}(.*):[^:]*$", out.strip()) if rc == 0 else None
        return (m.group(1) if m else "") or "/home/" + self.account

    def now(self):
        forced = self.environ.get("GAUGE_LOGIN_NOW")
        return float(forced) if forced else time.time()

    # -- what the box is configured to need
    def enabled(self, name):
        if name in ("claude", "claude2", "codex", "gemini", "opencode"):
            if self.env.get("AI_USAGE_ENABLED", "false") != "true":
                return False
            raw = self.env.get("AI_USAGE_SOURCES") or "codex,claude,opencode"
            return name in [s.strip() for s in raw.split(",") if s.strip()]
        if name == "google-health":
            return self.env.get("WEIGHT_ENABLED", "false") == "true"
        if name == "myair":
            return self.env.get("SLEEP_ENABLED", "false") == "true"
        return True

    def cred_path(self, name):
        knob = {"claude": "AI_USAGE_CLAUDE_CREDENTIALS", "claude2": "AI_USAGE_CLAUDE2_CREDENTIALS",
                "gemini": "AI_USAGE_GEMINI_CREDENTIALS", "opencode": "AI_USAGE_OPENCODE_AUTH",
                "google-health": "WEIGHT_TOKEN_FILE"}.get(name)
        default = {"claude": self.home + "/.claude/.credentials.json",
                   "claude2": self.home + "/.claude-2/.credentials.json",
                   "codex": self.codex_home + "/auth.json",
                   "gemini": self.home + "/.gemini/oauth_creds.json",
                   "opencode": self.home + "/.local/share/opencode/auth.json",
                   "google-health": "/var/lib/homehub-weight/tokens/google-health-token.json"}.get(name)
        return (self.env.get(knob) if knob else None) or default

    # -- journals
    def journal(self, unit, seam):
        forced = self.environ.get(seam)
        if forced:
            try:
                with open(forced, encoding="utf-8", errors="replace") as handle:
                    return handle.read()
            except OSError:
                return ""
        return run_quiet(["journalctl", "-u", unit, "-n", "2000", "--no-pager", "-o", "json"], 30)[1]


# --- credentials (presence and the one allowed field; never a value) ---------------------

def _nonempty(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def read_claude_credential(path):
    """{"exists", "signed_in", "deadline"(epoch seconds|None)}.

    `signed_in` is False for the file the CLI leaves when a login dies (accessToken and
    refreshToken emptied, expiresAt 0): that file stays non-empty, so size proves nothing.
    The token values are tested for emptiness here and go no further."""
    info = {"exists": False, "signed_in": False, "deadline": None}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return info
    if not raw.strip():
        return info
    info["exists"] = True
    try:
        oauth = json.loads(raw).get("claudeAiOauth")
    except (ValueError, AttributeError):
        return info
    if not isinstance(oauth, dict):
        return info
    info["signed_in"] = bool(oauth.get("accessToken")) and bool(oauth.get("refreshToken"))
    expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry > 0:
        info["deadline"] = expiry / 1000.0          # epoch MILLISECONDS in the file
    return info


def opencode_key_present(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for provider in PROVIDER_IDS:
        entry = data.get(provider)
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"]:
            return True
    return False


def gemini_auth_type(ctx):
    try:
        with open(ctx.home + "/.gemini/settings.json", encoding="utf-8") as handle:
            return json.load(handle)["security"]["auth"]["selectedType"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- feeder evidence -----------------------------------------------------------------------

def key_belongs(name, key):
    if name == "google-health":
        return key in GAUGES["google-health"]
    return key == "ai-usage-" + name or key.startswith("ai-usage-%s-" % name)


def auth_source(message):
    """The source a journal line says failed AUTHENTICATION, or None."""
    m = AUTH_USAGE.search(message) or AUTH_NOKEY.search(message)
    if m:
        return m.group(1)
    return "google-health" if AUTH_HEALTH.search(message) else None


def parse_invocations(text, line_re, end_re=None):
    """The journal (JSON lines) grouped by systemd invocation, oldest first.
    Each: {id, gauges{key:(state, posted)}, auth{sources}, complete(ts|None), first, last}."""
    order, by_id = [], {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        inv = entry.get("_SYSTEMD_INVOCATION_ID") or entry.get("INVOCATION_ID")
        if not inv:
            continue
        try:
            ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        except (TypeError, ValueError):
            ts = 0
        if inv not in by_id:
            by_id[inv] = {"id": inv, "gauges": {}, "auth": set(), "complete": None,
                          "first": ts, "last": ts}
            order.append(inv)
        rec = by_id[inv]
        if ts:
            rec["first"] = min(rec["first"], ts) if rec["first"] else ts
            rec["last"] = max(rec["last"], ts)
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            continue
        if end_re is not None and end_re.search(message):
            rec["complete"] = ts
        m = line_re.search(message)
        if m:
            rec["gauges"][m.group(1)] = (m.group(2), m.group(3) == "posted")
        failed_source = auth_source(message)
        if failed_source:
            rec["auth"].add(failed_source)
    return [by_id[i] for i in order]


def invocations_for(ctx, name):
    if KIND[name] == "health":
        return parse_invocations(ctx.journal("homehub-weight.service", "GAUGE_LOGIN_WEIGHT_LOG"),
                                 HEALTH_LINE, HEALTH_END)
    return parse_invocations(ctx.journal("homehub-ai-usage.service", "GAUGE_LOGIN_FEEDER_LOG"),
                             USAGE_LINE, CYCLE_END)


def _stamp(name, inv):
    """When an invocation finished, or None if it must not be used as evidence. A usage run
    ends with the feeder's own `cycle complete` line (one that never printed it died part-way
    or is still going). The weight unit prints no such line, so its end is the manager's
    `Deactivated successfully` / `Failed with result` line for the same invocation."""
    return inv["complete"]


def judge(name, inv):
    """(live gauges, post-failed live gauges, all this source's gauge lines) of one run."""
    mine = dict((k, v) for k, v in inv["gauges"].items() if key_belongs(name, k))
    live = [k for k, v in mine.items() if v[0] == "live"]
    failed = [k for k in live if not mine[k][1]]
    return live, failed, mine


def _running_now(ctx, name):
    """The invocation id of the feeder run going RIGHT NOW (systemctl is-active + InvocationID),
    or None. Seams: GAUGE_LOGIN_SERVICE_STATE / _INVOCATION_ID (usage unit) and the
    GAUGE_LOGIN_WEIGHT_ variants (weight unit)."""
    health = KIND[name] == "health"
    unit = "homehub-weight.service" if health else "homehub-ai-usage.service"
    prefix = "GAUGE_LOGIN_WEIGHT_" if health else "GAUGE_LOGIN_"
    state = ctx.environ.get(prefix + "SERVICE_STATE")
    if state is None:
        state = run_quiet(ctx.systemctl + ["is-active", unit], 10)[1].strip()
    if state not in ("active", "activating"):
        return None
    if prefix + "INVOCATION_ID" in ctx.environ:
        return ctx.environ[prefix + "INVOCATION_ID"] or None
    return run_quiet(ctx.systemctl + ["show", "-p", "InvocationID", "--value", unit], 10)[1].strip() or None


def feeder_evidence(ctx, name):
    """What the feeder last saw of this source, or None (= unknown).

    ai-cli-status.sh's rule, for BOTH units: the NEWEST invocation decides. One that never
    finished (no `cycle complete` / no manager end line) died part-way, so it means unknown
    and an older good run does not cover for it - except the run going right now
    (systemctl is-active + InvocationID match), where the previous finished run is the
    latest finished evidence.
    Usage unit: a run that printed no gauge line for the source did not read it (each source
    has its own schedule), so the walk goes back to the one that did, stopping at a run that
    did not finish and at the age limit. Weight unit: every run reads everything, so a
    finished newest run with no gauge lines (it exited before posting) is unknown."""
    now = ctx.now()
    health = KIND[name] == "health"
    limit = WEIGHT_EVIDENCE_MAX_AGE if health else USAGE_EVIDENCE_MAX_AGE
    runs = invocations_for(ctx, name)
    if not runs:
        return None
    newest = len(runs) - 1
    if runs[-1]["complete"] is None and len(runs) > 1 and _running_now(ctx, name) == runs[-1]["id"]:
        newest -= 1
    if runs[newest]["complete"] is None:
        return None
    for inv in reversed(runs[:newest + 1]):
        stamp = _stamp(name, inv)
        if stamp is None or now - stamp > limit:
            return None
        live, failed, mine = judge(name, inv)
        if mine:
            return {"live": live, "failed": failed, "gauges": mine, "at": stamp,
                    "auth": name in inv["auth"]}
        if health:
            return None
    return None


def first_run_after(ctx, name, since):
    """The completed runs that STARTED at or after `since` and read the source: the first
    one with a live gauge, else the newest one without (a run is judged on what it saw, and
    a later run may still prove the login), else None."""
    seen = None
    for inv in invocations_for(ctx, name):
        if _stamp(name, inv) is None or (inv["first"] or 0) < since:
            continue
        live, failed, mine = judge(name, inv)
        if mine:
            seen = {"live": live, "failed": failed, "gauges": mine, "auth": name in inv["auth"]}
            if live:
                return seen
    return seen


def journal_text_since(text, since):
    """The MESSAGE texts of a JSON journal at or after `since`."""
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            if int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6 >= since and isinstance(entry.get("MESSAGE"), str):
                out.append(entry["MESSAGE"])
        except (ValueError, TypeError, AttributeError):
            continue
    return chr(10).join(out)


# --- status --------------------------------------------------------------------------------

def fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(epoch))


def source_row(ctx, name):
    row = {"source": name, "label": LABELS[name], "state": "unknown", "flag": "", "deadline": None,
           "deadline_iso": None, "days_left": None, "gauges": GAUGES[name], "detail": "",
           "fix": "login " + name}
    if KIND[name] == "manual":
        if name == "myair" and not ctx.enabled("myair"):
            row.update(state="off", detail="SLEEP_ENABLED is not true", fix="")
        else:
            row.update(state="manual", detail="no tool-run sign-in; `login %s` prints the steps" % name)
        return row
    if not ctx.enabled(name):
        why = "WEIGHT_ENABLED is not true" if name == "google-health" else \
            ("AI_USAGE_ENABLED is not true" if ctx.env.get("AI_USAGE_ENABLED", "false") != "true"
             else "not in AI_USAGE_SOURCES")
        row.update(state="off", detail=why, fix="")
        return row
    path = ctx.cred_path(name)
    expired = False
    if name in ("claude", "claude2"):
        cred = read_claude_credential(path)
        if not cred["exists"]:
            row.update(state="owed", detail="no credential at " + path)
            return row
        if cred["deadline"] is not None:
            remaining = cred["deadline"] - ctx.now()
            row["deadline"] = cred["deadline"]
            row["deadline_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cred["deadline"]))
            row["days_left"] = round(remaining / DAY, 2)
            expired = remaining <= 0
            if 0 < remaining <= DUE_SOON_SECONDS:
                row["flag"] = "DUE SOON"
        if not cred["signed_in"]:
            row.update(state="DEAD", detail="the credential file is blanked: the login died")
            return row
        if expired:
            row.update(state="DEAD", detail="the refresh token expired %s" % fmt_time(cred["deadline"]))
            return row
    elif name == "opencode":
        if not opencode_key_present(path):
            row.update(state="owed", detail="no workspace key in " + path)
            return row
    elif name == "gemini":
        if not _nonempty(path):
            row.update(state="owed", detail="no credential at " + path)
            return row
        selected = gemini_auth_type(ctx)
        if selected != "oauth-personal":
            row.update(state="owed", detail="auth type is %s, not oauth-personal: sign in with "
                       "\"Login with Google\"" % (selected or "not selected"))
            return row
    else:
        if not _nonempty(path):
            row.update(state="owed", detail="no credential at " + path)
            return row
    ev = feeder_evidence(ctx, name)
    if ev is None:
        row["detail"] = "credential present; no feeder run read it in the last %d min" % (
            (WEIGHT_EVIDENCE_MAX_AGE if KIND[name] == "health" else USAGE_EVIDENCE_MAX_AGE) // 60)
    elif ev["live"]:
        if ev["failed"]:
            row.update(state="post-failing",
                       detail="the login works but a POST to the tracker failed (journalctl -u homehub-ai-usage)")
        else:
            row["state"] = "live"
            row["detail"] = "live: " + ", ".join(sorted(ev["live"]))
    elif ev["auth"]:
        row.update(state="DEAD", detail="the feeder reports an authentication failure for it")
    else:
        row.update(state="unavailable",
                   detail="the feeder could not read it this cycle; not a sign-in failure "
                          "(a throttle, a network error or a bad reply look the same as a dead "
                          "login in the gauges, but print no authentication failure)")
    return row


def build_status(ctx):
    return [source_row(ctx, n) for n in ORDER]


def render_table(rows):
    lines = []
    header = ("SOURCE", "STATE", "DEADLINE", "GAUGES")
    cells = []
    for r in rows:
        if r["deadline"] is not None:
            dl = "%s (%.1f d)" % (fmt_time(r["deadline"]), r["days_left"])
        else:
            dl = "-"
        state = r["state"] + ("  " + r["flag"] if r["flag"] else "")
        gauges = ", ".join(g.replace("ai-usage-", "") for g in r["gauges"]) or "-"
        cells.append((r["source"], state, dl, gauges))
    widths = [max(len(header[i]), max(len(c[i]) for c in cells)) for i in range(3)]
    fmt = "  ".join("%%-%ds" % w for w in widths) + "  %s"
    lines.append(fmt % header)
    for c in cells:
        lines.append(fmt % c)
    need = [r for r in rows if r["state"] in ("DEAD", "owed", "unknown", "post-failing", "unavailable")
            or r["flag"]]
    if need:
        lines.append("")
        for r in need:
            lines.append("%s: %s" % (r["source"], r["detail"]))
            if r["fix"]:
                lines.append("    fix: sudo python3 %s %s" % (os.path.join(HERE, "gauge_login.py"), r["fix"]))
    else:
        lines.append("")
        lines.append("Nothing is dead, owed or due soon.")
    return "\n".join(lines)


def cmd_status(ctx, as_json):
    rows = build_status(ctx)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(render_table(rows))
    return 0


# --- login ----------------------------------------------------------------------------------

INTRO = {
    "claude": "A link will be printed. Open it in a browser signed in to the account THIS RING "
              "tracks (account 1). The page then shows a code; paste it back into this window "
              "at the prompt. The CLI may exit 1 after printing 'Login successful.' - that is "
              "normal and is not the verdict; the feeder is.",
    "claude2": "A link will be printed. Open it signed in to the SECOND account (not account 1). "
               "The page shows a code; paste it back here. This runs with its own config "
               "directory so account 1 is left alone, and I check that afterwards.",
    "codex": "A link and a one-time code will be printed (the code lasts 15 minutes). Open the "
             "link in a browser, enter the code and approve. Nothing is pasted back here; the "
             "command ends by itself once you approve.",
    "gemini": "This opens Gemini's own screen. Choose 'Login with Google', open the link it "
              "prints and paste the code back. Then type ONE short prompt (it onboards the "
              "account; without it the gauge says 'no currentTier') and quit with /quit or Ctrl+C twice.",
    "opencode": "You will be asked to paste the OpenCode workspace key; it is hidden as you "
                "type and never shown. Create that key at opencode.ai with Permissions = Read "
                "only: it can then read the usage gauge but cannot spend on a model call.",
    "google-health": "A Google link will be printed. Open it and sign in as the household "
                     "account, leaving EVERY permission ticked. The browser then lands on "
                     "'This site can't be reached' at localhost:8117 - that page is SUCCESS. "
                     "Copy the WHOLE address bar and paste it here (a bare code is refused). "
                     "Read any 'NOT GRANTED' line it prints: it means a permission was unticked.",
}


def cli_login_argv(ctx, name):
    """The argv that runs the vendor's own sign-in as the AI account, from its home with the
    unit's environment (the `cd ~` matters: OpenCode walks up from the cwd)."""
    cmd, extras = {
        "claude": ("/usr/local/bin/claude auth login --claudeai", []),
        "claude2": ("/usr/local/bin/claude auth login --claudeai",
                    ["CLAUDE_CONFIG_DIR=%s/.claude-2" % ctx.home]),
        "codex": ("/usr/local/bin/codex login --device-auth", ["CODEX_HOME=" + ctx.codex_home]),
        "gemini": ("/usr/local/bin/gemini", ["NO_BROWSER=true"]),
    }[name]
    return (ctx.runas + ["env", "HOME=" + ctx.home, "PATH=" + PATH_ENV] + extras
            + ["sh", "-c", "cd ~ && exec " + cmd])


def mint_argv(ctx):
    forced = ctx.environ.get("GAUGE_LOGIN_MINT")
    if forced:
        return shlex.split(forced)
    return ["/usr/bin/python3", os.path.join(HERE, "..", "weight", "weight_oauth.py"), "mint", "--force"]


def spawn_interactive(argv, timeout=1800):
    """Run with the terminal inherited: no capture, so the Owner sees and answers the
    vendor's prompts directly. Returns the exit code (informational only)."""
    try:
        return subprocess.call(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124
    except OSError as exc:
        raise LoginError("could not start the sign-in (%s)" % exc.__class__.__name__)


# The OpenCode writer runs AS THE ACCOUNT (so the file belongs to it), reads the key from
# stdin (never argv, never the environment), and replaces the file atomically at mode 600.
# It prints nothing but a status word; the key is not in any message.
OPENCODE_WRITER = r'''
import json, os, sys
path = sys.argv[1]
providers = ("opencode-go", "opencode")
key = sys.stdin.readline().rstrip("\r\n")
if not key or any(c.isspace() for c in key):
    print("refused: empty key or whitespace in it"); sys.exit(2)
try:
    with open(path, encoding="utf-8") as h:
        data = json.load(h)
    if not isinstance(data, dict):
        print("refused: the existing file is not a JSON object"); sys.exit(2)
except FileNotFoundError:
    data = {}
except (OSError, ValueError):
    print("refused: the existing file is unreadable or not JSON; not overwriting"); sys.exit(2)
present = [p for p in providers if isinstance(data.get(p), dict)]
if present:
    for p in present:
        data[p]["key"] = key
else:
    data["opencode"] = {"type": "api", "key": key}
d = os.path.dirname(path)
os.makedirs(d, mode=0o700, exist_ok=True)
tmp = path + ".tmp-%d" % os.getpid()
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        json.dump(data, h)
        h.flush(); os.fsync(h.fileno())
    os.replace(tmp, path)
except BaseException:
    try: os.unlink(tmp)
    except OSError: pass
    raise
os.chmod(path, 0o600)
print("written")
'''


def write_opencode_key(ctx, key):
    path = ctx.cred_path("opencode")
    argv = ctx.runas + [ctx.python, "-c", OPENCODE_WRITER, path]
    try:
        done = subprocess.run(argv, input=key + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LoginError("could not run the key writer (%s)" % exc.__class__.__name__)
    word = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or word[-1:] != ["written"]:
        reason = word[-1] if word else "exit %d" % done.returncode
        raise LoginError("the key was NOT written: %s" % reason.replace(key, "***"))


def have_tty(ctx):
    if ctx.environ.get("GAUGE_LOGIN_ASSUME_TTY") == "1":
        return True
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def file_digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def account1_fingerprint(ctx):
    return (file_digest(ctx.home + "/.claude/.credentials.json"), file_digest(ctx.home + "/.claude.json"))


def tracker_url(ctx):
    domain = ctx.env.get("DOMAIN", "")
    sub = ctx.env.get("TRACKER_SUBDOMAIN", "") or "tracker"
    if not domain or domain.endswith("example.tld"):
        return "https://tracker.<your domain>/api/drive/connect"
    return "https://%s.%s/api/drive/connect" % (sub, domain)


def manual_step(ctx, name):
    if name == "tracker-sheet":
        return ("The tracker's Google Sheet link is browser-only. Open\n"
                "  %s\n"
                "signed in with Google as for the tracker, and approve the consent screen "
                "(about a minute). Ignore the tracker's own advice to run `tracker drive-setup-url`: "
                "that subcommand does not exist. Verify in the tracker log: `drive: stored a "
                "credential for ...`, then `drive: applied N item(s)`." % tracker_url(ctx))
    return MANUAL_STEPS[name]


def wait_for_verdict(ctx, name, since):
    """Poll for the first completed feeder run after `since` that read the source.
    Returns (verdict, message lines); verdict is "pass", "fail" (live nowhere and the feeder
    printed an authentication failure, no run at all, or a POST failure) or "pending" (the
    feeder ran but the vendor did not answer, and nothing says the login is bad)."""
    timeout = float(ctx.environ.get("GAUGE_LOGIN_VERIFY_SECONDS") or 180)
    poll = float(ctx.environ.get("GAUGE_LOGIN_POLL_SECONDS") or 10)
    unit = "homehub-weight.service" if KIND[name] == "health" else "homehub-ai-usage.service"
    # Ask for a run now rather than wait out the timer. A oneshot that exits 1 because some
    # OTHER source is unavailable is normal, so neither result of this is the verdict.
    if KIND[name] == "health":
        run_quiet(ctx.systemctl + ["start", unit], 120)
    else:
        run_quiet(ctx.systemctl + ["start", "--no-block", unit], 30)
    deadline = time.monotonic() + timeout
    seen = None
    while True:
        seen = first_run_after(ctx, name, since)
        if seen is not None and (seen["live"] or seen["auth"]):
            break                      # proven, or an authentication failure: no point waiting
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    if seen is None:
        return "fail", ["no completed feeder run read %s within %d s of the sign-in" % (name, int(timeout))]
    lines = []
    for key, (state, posted) in sorted(seen["gauges"].items()):
        lines.append("%s %s %s" % (key, state, "posted" if posted else "POST FAILED"))
    if not seen["live"]:
        if seen["auth"]:
            return "fail", ["the feeder reports an authentication failure for %s:" % name] + lines
        return "pending", ["signed in, but the vendor did not answer yet (not an auth failure) - "
                           "re-run status in a few minutes. What the feeder saw:"] + lines
    if seen["failed"]:
        return "fail", ["the login works but a live gauge's POST failed:"] + lines
    return "pass", lines


def login_one(ctx, name):
    """Returns 0 on PASS, 1 on FAIL. Raises LoginError for a refusal before anything runs."""
    if name in MANUAL_STEPS:
        print(manual_step(ctx, name))
        return 0
    if not have_tty(ctx):
        raise LoginError("login needs a terminal (the vendor prompts for a code or key). Run it "
                         "over `ssh -t`, or use RefreshGaugeLogins.cmd.")
    label = LABELS[name]
    print("")
    print("== %s ==" % label)
    print(INTRO[name])
    if not ctx.enabled(name):
        print("NOTE: %s is not enabled on this hub (%s), so signing in will not light a gauge "
              "until it is; there will be nothing to verify." % (name, source_row(ctx, name)["detail"]))
    guard = account1_fingerprint(ctx) if name == "claude2" else None
    since = None
    if name in ("claude", "claude2", "codex", "gemini"):
        rc = spawn_interactive(cli_login_argv(ctx, name), 900 if name == "codex" else 1800)
        print("(the sign-in command ended, exit %s; that is not the verdict)" % rc)
    elif name == "opencode":
        key = _read_secret("OpenCode workspace key (hidden): ")
        write_opencode_key(ctx, key)
        key = None
        print("key written to %s (mode 600, owned by %s)" % (ctx.cred_path("opencode"), ctx.account))
    elif name == "google-health":
        rc = spawn_interactive(mint_argv(ctx), 900)
        print("(mint ended, exit %s)" % rc)
    since = ctx.now()
    failed = False
    if guard is not None and account1_fingerprint(ctx) != guard:
        failed = True
        print("")
        print("!" * 72)
        print("!! ACCOUNT 1's CREDENTIAL OR ~/.claude.json CHANGED DURING THE ACCOUNT 2 SIGN-IN.")
        print("!! Account 1 may now be signed in as the wrong account. Run: login claude")
        print("!" * 72)
    if name == "gemini":
        selected = gemini_auth_type(ctx)
        print("gemini auth type: %s" % (selected or "not selected"))
        if selected != "oauth-personal":
            failed = True
            print("It must be oauth-personal: sign in again with 'Login with Google'.")
        run_quiet(ctx.systemctl + ["start", "homehub-gemini-refresh.service"], 60)
        keeper = ctx.journal("homehub-gemini-refresh.service", "GAUGE_LOGIN_GEMINI_LOG")
        if "DEAD LOGIN" in journal_text_since(keeper, since):
            failed = True
            print("homehub-gemini-refresh reports DEAD LOGIN: the sign-in did not take.")
    if name in ("claude", "claude2"):
        cred = read_claude_credential(ctx.cred_path(name))
        if cred["deadline"]:
            print("new deadline: %s (%.1f days)" % (fmt_time(cred["deadline"]),
                                                    (cred["deadline"] - ctx.now()) / DAY))
    if not ctx.enabled(name):
        print("RESULT: signed in; not verified (source not enabled).")
        return 1 if failed else 0
    print("verifying through the feeder (up to %s s)..." % (ctx.environ.get("GAUGE_LOGIN_VERIFY_SECONDS") or 180))
    verdict, lines = wait_for_verdict(ctx, name, since)
    for line in lines:
        print("  " + line)
    if verdict == "pending" and not failed:
        print("RESULT: NOT YET VERIFIED (signed in; the vendor has not answered)")
        return 1
    passed = verdict == "pass" and not failed
    print("RESULT: %s" % ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


_read_secret = getpass.getpass


def cmd_login(ctx, source):
    name = ALIASES.get(source, source)
    if name not in ORDER:
        print("unknown source %r; choose one of: %s" % (source, ", ".join(ORDER)), file=sys.stderr)
        return 2
    try:
        return login_one(ctx, name)
    except LoginError as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(chr(10) + "aborted - nothing verified", file=sys.stderr)
        return 130


def cmd_menu(ctx):
    if not have_tty(ctx):
        print("REFUSED: the menu needs a terminal. Use `status` for the table, or run over `ssh -t`.",
              file=sys.stderr)
        return 2
    try:
        rows = build_status(ctx)
    except (KeyboardInterrupt, EOFError):
        print(chr(10) + "aborted - nothing verified", file=sys.stderr)
        return 130
    print(render_table(rows))
    worst = 0
    # Ask only about what is dead or due: owed sources are deliberate deferrals (Claude 2,
    # Gemini) and `unavailable` is not a sign-in problem, so those are listed, not asked.
    asked = [r for r in rows if r["state"] == "DEAD" or r["flag"]]
    listed = [r["source"] for r in rows if r["state"] in ("owed", "unavailable")]
    try:
        for r in asked:
            why = r["state"] + ("/" + r["flag"] if r["flag"] else "")
            answer = input("\nSign in %s now (%s)? [y/N] " % (r["source"], why)).strip().lower()
            if answer in ("y", "yes"):
                rc = cmd_login(ctx, r["source"])
                if rc == 130:
                    return rc
                worst = max(worst, rc)
        if not asked:
            print("\nNothing is dead or due soon.")
        if listed:
            print("\nNot asked about (owed or unavailable): %s. To sign one in, type its name below "
                  "or run: login <source>." % ", ".join(listed))
        while True:
            extra = input("\nSign in a source by name (%s), or Enter to finish: " % ", ".join(ORDER)).strip()
            if not extra:
                break
            rc = cmd_login(ctx, extra)
            if rc == 130:
                return rc
            worst = max(worst, rc)
    except (KeyboardInterrupt, EOFError):
        print("\naborted - nothing verified", file=sys.stderr)
        return 130
    return worst


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gauge_login.py", description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="status as JSON")
    sub = parser.add_subparsers(dest="cmd")
    p_status = sub.add_parser("status")
    p_status.add_argument("--json", action="store_true", dest="json_sub")
    p_login = sub.add_parser("login")
    p_login.add_argument("source")
    sub.add_parser("menu")
    args = parser.parse_args(argv)
    # One guard around init AND dispatch: Ctx() and build_status() shell out and read journals,
    # and a Ctrl+C there must not print a traceback either.
    try:
        ctx = Ctx()
        if args.cmd == "status":
            return cmd_status(ctx, args.json or args.json_sub)
        if args.cmd == "login":
            return cmd_login(ctx, args.source)
        if args.json:
            return cmd_status(ctx, True)
        return cmd_menu(ctx)
    except (KeyboardInterrupt, EOFError):
        print(chr(10) + "aborted - nothing verified", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
