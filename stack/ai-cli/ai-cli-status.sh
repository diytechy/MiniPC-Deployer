#!/usr/bin/env bash
# ai-cli-status.sh - what is installed, and which logins are still owed.
#
# The Owner's rule (2026-10-11): the box provisions the CLIs its configuration
# needs; only the vendor SIGN-INS stay human. So this prints no package
# complaint - it prints PROVISIONING progress and the LOGINS that remain, each
# with the exact command, and nothing else.
#
# TWO LEVELS, never conflated (plan 3.6):
#   INSTALLED                    from `provision-ai-clis.sh --status --json`.
#   CONSUMER OPERATION VERIFIED  the real consumer did its job: the usage
#                                feeder's last cycle logged `live` for a gauge
#                                of that source. Until then every CLI reads
#                                "installed, operation not yet verified".
#
# LOGIN STATES, each credential checked AS THE ACCOUNT (runuser -u ACCT --
# test -r / -s); the file's CONTENT is never read:
#   login owed | permissions wrong | credential present (not verified) | verified
#   plus unsupported-path (the knob differs from where sign-in writes it) and,
#   for gemini, `auth type not selected` / `wrong auth type <x>`.
#
# Always exits 0: a diagnostic must not be able to fail the thing it reports on.
#
# Seams (tests; the defaults are the production values):
#   AI_CLI_ENV_FILE      the .env                      ($HERE/../.env)
#   AI_CLI_PROVISIONER   provision-ai-clis.sh          ($HERE/provision-ai-clis.sh)
#   AI_CLI_ROUTES_FILE   routes-enabled                (AI_CLI_ENABLED_ROUTES, else $HERE)
#   AI_CLI_REGISTRY_FILE agents.registry.csv           (AI_CLI_REGISTRY, else $HERE)
#   AI_CLI_RUNAS         command prefix to act as the account ("runuser -u ACCT --")
#   AI_CLI_FEEDER_LOG    a file standing in for the feeder's journal
#   AI_CLI_CODEX_USAGE_HOME  the usage unit's CODEX_HOME   (/var/lib/homehub-ai-codex)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AI_CLI_STATUS_HERE="$HERE"
exec python3 - "$@" <<'PY'
import csv
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.environ["AI_CLI_STATUS_HERE"]
PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
CODEX_USAGE_HOME = os.environ.get("AI_CLI_CODEX_USAGE_HOME", "/var/lib/homehub-ai-codex")


def load_env():
    env = dict(os.environ)
    path = os.environ.get("AI_CLI_ENV_FILE") or os.path.join(HERE, "..", ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env[key.strip()] = value
    except OSError:
        pass
    return env


def run(cmd, timeout=30):
    # Only a Windows dev PC (the tests, under Git Bash) needs this: a Windows
    # python does not resolve extensionless shell commands, bash does.
    if os.name == "nt":
        cmd = ["bash", "-c", 'exec "$@"', "_"] + list(cmd)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


ENV = load_env()
CLI_ENABLED = ENV.get("AI_CLI_ENABLED", "false") == "true"
USAGE_ENABLED = ENV.get("AI_USAGE_ENABLED", "false") == "true"

# ONE effective account: the rule lives in ensure-ai-account.sh --effective.
rc, out = run(["bash", os.path.join(HERE, "ensure-ai-account.sh"), "--effective",
               "true" if CLI_ENABLED else "false", ENV.get("AI_CLI_USER", ""),
               ENV.get("AI_USAGE_USER_ACCOUNT", "")])
ACCOUNT = out.strip() or "homehub-ai"

rc, out = run(["getent", "passwd", ACCOUNT])
_passwd = re.match(r"^(?:[^:]*:){5}(.*):[^:]*$", out.strip()) if rc == 0 else None
HOME = _passwd.group(1) if _passwd else ""
HOME = HOME or "/home/" + ACCOUNT

RUNAS = shlex.split(os.environ["AI_CLI_RUNAS"]) if os.environ.get("AI_CLI_RUNAS") \
    else ["runuser", "-u", ACCOUNT, "--"]


def as_account(*cmd):
    return run(RUNAS + list(cmd), timeout=20)


# --- what the box is configured to need --------------------------------------

USAGE_CLI = {"claude": "claude", "claude2": "claude", "codex": "codex",
             "gemini": "gemini", "opencode": "opencode"}
# (source, knob, supported path under HOME)
USAGE_KNOBS = {
    "claude": ("AI_USAGE_CLAUDE_CREDENTIALS", HOME + "/.claude/.credentials.json"),
    "claude2": ("AI_USAGE_CLAUDE2_CREDENTIALS", HOME + "/.claude-2/.credentials.json"),
    "gemini": ("AI_USAGE_GEMINI_CREDENTIALS", HOME + "/.gemini/oauth_creds.json"),
    "opencode": ("AI_USAGE_OPENCODE_AUTH", HOME + "/.local/share/opencode/auth.json"),
}


def enabled_openai_or_anthropic():
    """Families of the routes listed in AI_CLI_ENABLED_ROUTES (uncommented rows)."""
    routes_path = os.environ.get("AI_CLI_ROUTES_FILE")
    if not routes_path:
        knob = ENV.get("AI_CLI_ENABLED_ROUTES", "")
        routes_path = knob if knob and os.path.exists(knob) else os.path.join(HERE, "routes-enabled")
    registry_path = os.environ.get("AI_CLI_REGISTRY_FILE")
    if not registry_path:
        knob = ENV.get("AI_CLI_REGISTRY", "")
        registry_path = knob if knob and os.path.exists(knob) else os.path.join(HERE, "agents.registry.csv")
    ids = []
    try:
        with open(routes_path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.split("#", 1)[0].strip()
                if line:
                    ids.append(line.split()[0])
    except OSError:
        return set()
    families = {}
    try:
        with open(registry_path, encoding="utf-8", newline="") as handle:
            lines = [l for l in handle if l.strip() and not l.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            families[row.get("Id", "")] = (row.get("Family") or "").upper()
    except OSError:
        return set()
    return {families[i] for i in ids if i in families}


# Each credential to check: dict(label, cli, path, knob, default, recipe_kind, gauge_source)
checks = []
sources = []
if USAGE_ENABLED:
    raw = ENV.get("AI_USAGE_SOURCES") or "codex,claude,opencode"
    sources = [s.strip() for s in raw.split(",") if s.strip()]
for src in sources:
    cli = USAGE_CLI.get(src)
    if cli is None:
        continue                       # grok / freellmapi: no CLI, no credential here
    if src == "codex":
        checks.append(dict(label="codex (usage)", cli="codex", knob=None,
                           path=CODEX_USAGE_HOME + "/auth.json", default=None,
                           kind="codex-usage", source="codex"))
        continue
    knob, default = USAGE_KNOBS[src]
    checks.append(dict(label=src, cli=cli, knob=knob, path=ENV.get(knob, ""),
                       default=default, kind=src, source=src))
if CLI_ENABLED:
    families = enabled_openai_or_anthropic()
    if "ANTHROPIC" in families and not any(c["path"] == HOME + "/.claude/.credentials.json"
                                           for c in checks):
        checks.append(dict(label="claude (ai-cli)", cli="claude", knob=None,
                           path=HOME + "/.claude/.credentials.json", default=None,
                           kind="claude", source=None))
    if "OPENAI" in families:
        checks.append(dict(label="codex (ai-cli)", cli="codex", knob=None,
                           path=HOME + "/.codex/auth.json", default=None,
                           kind="codex-cli", source=None))

# --- the provisioner's status (level 1) --------------------------------------

provisioner = os.environ.get("AI_CLI_PROVISIONER") or os.path.join(HERE, "provision-ai-clis.sh")
status = None
if os.path.exists(provisioner):
    rc, out = run(["bash", provisioner, "--status", "--json"], timeout=60)
    try:
        status = json.loads(out)
        if not isinstance(status, dict):
            status = None
    except ValueError:
        status = None

PROV_TEXT = {
    "complete": "complete",
    "incomplete": "INCOMPLETE - retrying automatically; CLIs already on the box keep working meanwhile",
    "running": "in progress",
    "off": "off (AI_CLI_PROVISION=off)",
}
clis = (status or {}).get("clis") or {}
if status is None:
    print("PROVISIONING: status unavailable right now (the provisioner gave no status)")
else:
    state = str(status.get("provisioning") or "")
    print("PROVISIONING: " + PROV_TEXT.get(state, state or "unknown"))

# --- the feeder's last cycle (level 2) ---------------------------------------


def feeder_live_sources():
    log_file = os.environ.get("AI_CLI_FEEDER_LOG")
    if log_file:
        try:
            with open(log_file, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            text = ""
    else:
        rc, text = run(["journalctl", "-u", "homehub-ai-usage.service", "-n", "400",
                        "--no-pager", "-o", "cat"], timeout=20)
    last = {}
    for line in text.splitlines():
        m = re.search(r"ai-usage:\s+(ai-usage-([a-z0-9]+)-\S+)\s+(live|UNAVAILABLE)", line)
        if m:
            last[m.group(1)] = (m.group(2), m.group(3))
    return {src for (src, state) in last.values() if state == "live"}


LIVE = feeder_live_sources()

# --- the logins ---------------------------------------------------------------


def owner_mode(path):
    rc, out = run(["stat", "-c", "%U:%G %a", path])
    parts = out.split()
    return (parts[0], parts[1]) if rc == 0 and len(parts) == 2 else (None, None)


def recipe(check, reason):
    base = "sudo -u %s env HOME=%s PATH=%s " % (ACCOUNT, HOME, PATH_ENV)
    kind = check["kind"]
    if kind == "claude":
        return base + "sh -c 'cd ~ && /usr/local/bin/claude auth login --claudeai'"
    if kind == "claude2":
        return (base + "CLAUDE_CONFIG_DIR=%s/.claude-2 " % HOME
                + "sh -c 'cd ~ && /usr/local/bin/claude auth login --claudeai'")
    if kind == "codex-usage":
        return base + "CODEX_HOME=%s /usr/local/bin/codex login --device-auth" % CODEX_USAGE_HOME
    if kind == "codex-cli":
        return base + "/usr/local/bin/codex login --device-auth"
    if kind == "opencode":
        return base + "sh -c 'cd ~ && /usr/local/bin/opencode auth login'"
    if kind == "gemini":
        return (base + "NO_BROWSER=true sh -c 'cd ~ && /usr/local/bin/gemini'")
    return None


def gemini_auth_type():
    rc, out = as_account("cat", HOME + "/.gemini/settings.json")
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        return data["security"]["auth"]["selectedType"]
    except (ValueError, KeyError, TypeError):
        return None


results = []      # (label, state, detail lines, owed?)
for c in checks:
    label, path = c["label"], c["path"]
    if c["knob"] is not None and path != c["default"]:
        results.append((label, "unsupported-path",
                        ["set %s=%s" % (c["knob"], c["default"])], True))
        continue
    present = as_account("test", "-s", path)[0] == 0 and as_account("test", "-r", path)[0] == 0
    if not present:
        if os.path.exists(path):
            owner, mode = owner_mode(path)
            fix = ["owner/mode now %s %s; the account %s cannot read it" % (owner or "?", mode or "?", ACCOUNT),
                   "sudo chown %s:%s %s && sudo chmod 600 %s" % (ACCOUNT, ACCOUNT, path, path)]
            results.append((label, "permissions wrong", fix, True))
        else:
            results.append((label, "login owed", [recipe(c, "owed")], True))
        continue
    if c["kind"] == "gemini":
        selected = gemini_auth_type()
        if selected is None:
            results.append((label, "auth type not selected",
                            ['sign in with "Login with Google"', recipe(c, "type")], True))
            continue
        if selected != "oauth-personal":
            results.append((label, "wrong auth type %s" % selected,
                            ['sign in with "Login with Google"', recipe(c, "type")], True))
            continue
    if c["source"] and c["source"] in LIVE:
        results.append((label, "verified", [], False))
    else:
        results.append((label, "credential present (not verified)", [], False))

# --- the CLIs (level 1 + level 2 per CLI) -------------------------------------

needed = []
for c in checks:
    if c["cli"] not in needed:
        needed.append(c["cli"])
verified_clis = {c["cli"] for c, r in zip(checks, results) if r[1] == "verified"}
for key in needed:
    info = clis.get(key) or {}
    if info.get("installed"):
        version = info.get("version") or ""
        tail = "consumer operation verified" if key in verified_clis \
            else "installed, operation not yet verified"
        print("  %s %s: %s" % (key, version, tail) if version else "  %s: %s" % (key, tail))
    elif status is not None:
        reason = info.get("pending_reason")
        print("  %s: being provisioned%s" % (key, " (%s)" % reason if reason else ""))
    else:
        print("  %s: provisioning state unavailable" % key)

for label, state, lines, owed in results:
    print("  login %s: %s" % (label, state))

owed = [(l, s, d) for (l, s, d, o) in results if o]
if owed:
    print("LOGINS OWED: " + ", ".join("%s (%s)" % (l, s) for l, s, d in owed))
    for l, s, d in owed:
        print("  %s - %s" % (l, s))
        for line in d:
            print("    " + line)
else:
    print("LOGINS OWED: none")
PY
