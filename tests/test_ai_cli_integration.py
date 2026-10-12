"""TC-1205..TC-1214 - the AI CLI integration: one account, the status step, the
firstboot hook, the updater files and the credential-path contract (SR-079,
SR-080; LLR-1113..LLR-1120).

Owner, 2026-10-11: the box provisions what its configuration needs instead of
erroring on a missing account or CLI; only the vendor sign-ins stay human.

Everything runs the REAL scripts against stubbed system commands (id, useradd,
getent, runuser, systemctl, install). Each assertion is made against the OTHER
side of its contract: the path read from the real unit file, the feeder's own
env reader, the real routes-enabled + registry, the JSON the files parse to.
Provisioning itself is a FIXTURE standing in for provision-ai-clis.sh that
implements only the contract (`--schedule`, `--status [--json]`); the run
against the real provisioner is test_real_provisioner_contract (skipped, loudly,
until that script exists in the tree).

Verifies: TC-1205..TC-1214 (SR-079, SR-080; LLR-1113..LLR-1120)
"""

import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI_DIR = REPO / "stack" / "ai-cli"
USAGE_DIR = REPO / "stack" / "ai-usage"
FIRSTBOOT = REPO / "stack" / "autoinstall" / "firstboot.sh"
ENV_EXAMPLE = REPO / "stack" / ".env.example"

ENSURE = CLI_DIR / "ensure-ai-account.sh"
STATUS = CLI_DIR / "ai-cli-status.sh"
SETUP_CLI = CLI_DIR / "setup-ai-cli.sh"
SETUP_USAGE = USAGE_DIR / "setup-ai-usage.sh"
UPDATER = CLI_DIR / "install-updater-controls.sh"

CANARY = "CANARY-SECRET-9d41c7a0e5b3"
PATH_STRING = "/usr/local/bin:/usr/bin:/bin"
UNHAPPY = re.compile(r"missing|not found|refus", re.IGNORECASE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


feeder = _load("ai_usage_feeder_integration", USAGE_DIR / "ai_usage_feeder.py")
service = _load("ai_cli_service_integration", CLI_DIR / "ai_cli_service.py")


# ── the sandbox ──────────────────────────────────────────────────────────────

STUBS = {
    # `id -u` -> 0 (we are "root"); `id -u NAME` -> does NAME exist; `id -nG NAME`.
    "id": r"""#!/usr/bin/env bash
if [ "$1" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
if [ "$1" = "-u" ]; then grep -qx -- "$2" "$SANDBOX/accounts" 2>/dev/null; exit $?; fi
if [ "$1" = "-nG" ]; then cat "$SANDBOX/groups.$2" 2>/dev/null || echo "$2"; exit 0; fi
exit 0
""",
    "useradd": r"""#!/usr/bin/env bash
name="${@: -1}"
echo "useradd $name" >> "$SANDBOX/calls.log"
echo "$name" >> "$SANDBOX/accounts"
""",
    "getent": r"""#!/usr/bin/env bash
base="${STUB_HOME_BASE:-$SANDBOX/home}"
echo "$2:x:1001:1001::$base/$2:/bin/bash"
""",
    # Acts as the account by running the command, except that a path listed in
    # $SANDBOX/deny is unreadable to it (a root-owned 0600 file).
    "runuser": r"""#!/usr/bin/env bash
echo "runuser $*" >> "$SANDBOX/calls.log"
shift 3
if [ "$1" = "test" ] && [ -f "$SANDBOX/deny" ] && grep -qxF -- "$3" "$SANDBOX/deny"; then exit 1; fi
if [ "$1" = "env" ]; then exit 0; fi
exec "$@"
""",
    # STUB_SYSTEMCTL_FAIL=<substring>: any call whose arguments contain it fails.
    # STUB_ACTIVE=<state>: `is-active` answers that state (and exits 3) forever.
    # It also MODELS which timers are enabled-and-active ($SANDBOX/enabled, one unit
    # per line): `enable [--now] U` adds U, `disable [--now] U` removes it.
    # STUB_DISABLE_HALF=1: a failing `disable` has already removed the unit (it is
    # two steps in real systemd, so a failure can leave it half done).
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$SANDBOX/calls.log"
touch "$SANDBOX/enabled" "$SANDBOX/enabledonly"
failmatch=0
case "$*" in *"${STUB_SYSTEMCTL_FAIL:-@@none@@}"*) failmatch=1 ;; esac
if [ "$1" = "is-active" ] || [ "$1" = "is-enabled" ]; then
  [ "$failmatch" = 1 ] && exit 1
  # STUB_QFAIL_AFTER=<call>: once that call has happened, every state query fails.
  if [ -n "${STUB_QFAIL_AFTER:-}" ] && grep -qF -- "systemctl $STUB_QFAIL_AFTER" "$SANDBOX/calls.log"; then exit 1; fi
  unit="$2"
  case "$unit" in
    *.service)
      if [ "$1" = "is-active" ]; then echo "${STUB_ACTIVE:-inactive}"; exit 3; fi
      echo disabled; exit 1 ;;
  esac
  on=0; grep -qx -- "$unit" "$SANDBOX/enabled" && on=1
  eo=0; grep -qx -- "$unit" "$SANDBOX/enabledonly" && eo=1
  if [ "$1" = "is-active" ]; then
    if [ "$on" = 1 ]; then echo active; exit 0; fi
    echo inactive; exit 3
  fi
  if [ "$on" = 1 ] || [ "$eo" = 1 ]; then echo enabled; exit 0; fi
  echo disabled; exit 1
fi
unit="${@: -1}"
case "$1" in
  enable)
    if [ "$failmatch" = 1 ]; then
      # STUB_ENABLE_HALF: the unit ends up enabled AND running, yet the call fails.
      # STUB_ENABLE_NOSTART: it ends up enabled but NOT started (enable ok, start failed).
      [ -n "${STUB_ENABLE_HALF:-}" ] && echo "$unit" >> "$SANDBOX/enabled"
      [ -n "${STUB_ENABLE_NOSTART:-}" ] && echo "$unit" >> "$SANDBOX/enabledonly"
      exit 1
    fi
    grep -qx -- "$unit" "$SANDBOX/enabled" || echo "$unit" >> "$SANDBOX/enabled" ;;
  disable)
    if [ "$failmatch" = 1 ] && [ -z "${STUB_DISABLE_HALF:-}" ]; then exit 1; fi
    for f in enabled enabledonly; do
      grep -vx -- "$unit" "$SANDBOX/$f" > "$SANDBOX/$f.tmp"; mv "$SANDBOX/$f.tmp" "$SANDBOX/$f"
    done
    [ "$failmatch" = 1 ] && exit 1 ;;
  *) [ "$failmatch" = 1 ] && exit 1 ;;
esac
exit 0
""",
    "install": r"""#!/usr/bin/env bash
echo "install $*" >> "$SANDBOX/calls.log"
dirmode=0; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dirmode=1 ;;
    -m|-o|-g) shift ;;
    -*) ;;
    *) args+=("$1") ;;
  esac
  shift
done
if [ "$dirmode" = 1 ]; then
  for d in "${args[@]}"; do case "$d" in "$SANDBOX"*) mkdir -p "$d" ;; esac; done
else
  case "${args[1]}" in "$SANDBOX"*) cp "${args[0]}" "${args[1]}" ;; esac
fi
""",
    # `stat -c %U path` -> STUB_STAT_OWNER (else the first account created); the
    # status step's `-c "%U:%G %a"` form answers a root-owned 0600 file.
    # Git Bash has no flock; W1's --schedule only needs the lock to be takeable here
    # (the real locking is W1's own test suite, run under WSL).
    "flock": r"""#!/usr/bin/env bash
exit 0
""",
    "stat": r"""#!/usr/bin/env bash
if [ "$2" = "%U" ]; then echo "${STUB_STAT_OWNER:-$(head -1 "$SANDBOX/accounts")}"; exit 0; fi
echo "root:root 600"
""",
}

# A stand-in for provision-ai-clis.sh that implements ONLY the contract.
PROVISIONER = r"""#!/usr/bin/env bash
case "$1" in
  --schedule) echo "provisioner --schedule" >> "$SANDBOX/calls.log" ;;
  --run) echo "provisioner --run" >> "$SANDBOX/calls.log" ;;
  --status) cat "$SANDBOX/status.json" ;;
esac
exit 0
"""

ALL_INSTALLED = {
    "provisioning": "complete",
    "clis": {k: {"installed": True, "identity": k + "-id", "version": "1.0.0",
                 "link_target": "/usr/local/bin/" + k, "activation_state": "committed",
                 "pending_reason": None}
             for k in ("claude", "codex", "opencode", "gemini", "node")},
}


def _bash(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no POSIX bash")
    probe = subprocess.run([bash, "-c", 'test -d "$1" && echo yes', "_", tmp_path.as_posix()],
                           capture_output=True, text=True)
    if probe.stdout.strip() != "yes":
        pytest.skip("no POSIX bash that can see the test sandbox")
    return bash


class Box(object):
    def __init__(self, tmp_path, env_lines, accounts=(), status=ALL_INSTALLED):
        self.bash = _bash(tmp_path)
        self.root = tmp_path / "sandbox"
        self.units = self.root / "units"
        self.stubs = tmp_path / "stubs"
        self.home = self.root / "home"
        for d in (self.root, self.units, self.stubs, self.home):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in STUBS.items():
            self._write(self.stubs / name, body)
        py = Path(sys.executable).as_posix()
        self._write(self.stubs / "python3", '#!/usr/bin/env bash\nexec "%s" "$@"\n' % py)
        self._write(self.root / "provision-ai-clis.sh", PROVISIONER)
        (self.root / "accounts").write_text("".join(a + "\n" for a in accounts), encoding="utf-8")
        (self.root / "status.json").write_text(json.dumps(status), encoding="utf-8")
        (self.root / "feeder.log").write_text("", encoding="utf-8")
        self.env_file = tmp_path / "stack.env"
        self.env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        self.codex_home = self.root / "codex-home"

    @staticmethod
    def _write(path, body):
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        path.chmod(0o755)

    def env(self, **extra):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("AI_CLI_", "AI_USAGE_")):
                del env[key]
        env.update({
            "PATH": self.stubs.as_posix() + os.pathsep + env["PATH"],
            "SANDBOX": self.root.as_posix(),
            "AI_USAGE_ENV_FILE": self.env_file.as_posix(),
            "AI_CLI_ENV_FILE": self.env_file.as_posix(),
            "AI_USAGE_UNIT_DIR": self.units.as_posix(),
            "AI_CLI_UNIT_DIR": self.units.as_posix(),
            "AI_CLI_PROVISIONER": (self.root / "provision-ai-clis.sh").as_posix(),
            "AI_CLI_FEEDER_LOG": (self.root / "feeder.log").as_posix(),
            "AI_CLI_CODEX_USAGE_HOME": self.codex_home.as_posix(),
            "AI_CLI_NOW": str(NOW),
            "AI_CLI_USAGE_SERVICE_STATE": "inactive",
            # Git Bash rewrites "/home" into a Windows path when it crosses into a
            # native process; the stub getent must see the literal value.
            "MSYS2_ENV_CONV_EXCL": "STUB_HOME_BASE",
            "AI_USAGE_MIGRATION_POLL_SECONDS": "0.05",
            "AI_USAGE_MIGRATION_WAIT_SECONDS": "1",
        })
        env.update(extra)
        return env

    def run(self, script, *args, **extra):
        done = subprocess.run([self.bash, Path(script).as_posix()] + list(args),
                              env=self.env(**extra), capture_output=True, text=True)
        return done

    @property
    def accounts(self):
        path = self.root / "accounts"
        return [l for l in path.read_text(encoding="utf-8").splitlines() if l]

    @property
    def calls(self):
        path = self.root / "calls.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def journal(self, *invocations):
        """Write the feeder's journal as `journalctl -o json` lines. Each argument
        is a dict: gauges=[(id, state[, "POST FAILED"])...], age=seconds before NOW
        the invocation ended, complete=bool (did it print its `cycle complete` line),
        extra=[msg], id=<invocation id>, manager_only=True (only the manager's
        INVOCATION_ID lines exist: it died before the feeder printed anything)."""
        rows = []
        for n, inv in enumerate(invocations):
            iid = inv.get("id", "inv%02d" % n)
            ts = NOW - inv.get("age", 60)
            if inv.get("manager_only"):
                rows.append(json.dumps({"MESSAGE": "homehub-ai-usage.service: Failed with result 'exit-code'.",
                                        "INVOCATION_ID": iid,
                                        "__REALTIME_TIMESTAMP": str(int(ts * 1e6))}))
                continue
            lines = []
            for g in inv.get("gauges", []):
                lines.append("ai-usage: %-32s %-12s %s" % (g[0], g[1], g[2] if len(g) > 2 else "posted"))
            lines += inv.get("extra", [])
            if inv.get("complete", True):
                lines.append("ai-usage: cycle complete gauges=%d failures=0" % len(inv.get("gauges", [])))
            for k, text in enumerate(lines):
                rows.append(json.dumps({"MESSAGE": text, "_SYSTEMD_INVOCATION_ID": iid,
                                        "__REALTIME_TIMESTAMP": str(int((ts - len(lines) + k) * 1e6))}))
        (self.root / "feeder.log").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def cred(self, account, relative, content=CANARY):
        path = self.home / account / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


NOW = 1_800_000_000
USAGE_ENV = ["AI_USAGE_ENABLED=true", "AI_USAGE_USER=108000000000000000001",
             "AI_USAGE_FEED_URL=http://127.0.0.1:8787/api/feed"]


def all_text(*dones):
    return "".join(d.stdout + d.stderr for d in dones)


def tree_text(root):
    chunks = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


# ═══════════════════════════════════════════════════════════════════════════
# TC-1205 - ONE effective account (LLR-1113)
# ═══════════════════════════════════════════════════════════════════════════

def test_cli_only_certifies_alice_and_never_the_default_usage_name_llr1113(tmp_path):
    box = Box(tmp_path, ["AI_CLI_ENABLED=true", "AI_CLI_USER=alice",
                         "AI_USAGE_ENABLED=false", "AI_USAGE_USER_ACCOUNT=homehub-ai"])
    done = box.run(SETUP_CLI)
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.accounts == ["alice"]
    assert "useradd alice" in box.calls and not [c for c in box.calls if re.search(r"useradd homehub-ai|-o homehub-ai|-g homehub-ai|User=homehub-ai", c)]
    dropin = (box.units / "homehub-ai-cli.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "User=alice" in dropin and "Group=alice" in dropin
    assert not (box.units / "homehub-ai-usage.service").exists()


def test_usage_only_with_a_custom_name_creates_that_account_llr1113(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_USER_ACCOUNT=bob", "AI_USAGE_SOURCES=claude"])
    done = box.run(SETUP_USAGE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.accounts == ["bob"]
    dropin = (box.units / "homehub-ai-usage.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "User=bob" in dropin and "Group=bob" in dropin


def test_both_enabled_with_different_names_use_one_account_everywhere_llr1113(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_USER_ACCOUNT=bob", "AI_USAGE_SOURCES=claude,claude2,gemini",
                                     "AI_CLI_ENABLED=true", "AI_CLI_USER=alice"])
    # A drive-less home: `cut -d:` in setup-ai-usage.sh cannot split "C:/...".
    usage = box.run(SETUP_USAGE, STUB_HOME_BASE="/home")
    cli = box.run(SETUP_CLI, STUB_HOME_BASE="/home")
    assert usage.returncode == 0, usage.stdout + usage.stderr
    assert cli.returncode == 0, cli.stdout + cli.stderr          # no refusal either way
    assert box.accounts == ["alice"]
    text = all_text(usage, cli) + tree_text(box.units) + "\n".join(box.calls)
    # The differing usage name appears NOWHERE: output, drop-ins, ownership, recipes.
    assert "bob" not in text
    # ONE log line naming the effective account.
    assert len([l for l in usage.stdout.splitlines() if "share one account" in l]) == 1
    assert "alice" in [l for l in usage.stdout.splitlines() if "share one account" in l][0]
    # Both units' User= agree, and every refresher drop-in uses it and its home.
    users = set()
    for dropin in box.units.rglob("10-account.conf"):
        found = re.findall(r"^User=(.*)$", dropin.read_text(encoding="utf-8"), re.M)
        assert found == ["alice"], (dropin, found)
        users.update(found)
    assert users == {"alice"}
    home = "/home/alice"
    claude = (box.units / "homehub-claude-refresh@claude.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "ReadWritePaths=-%s/.claude -%s/.claude.json" % (home, home) in claude
    claude2 = (box.units / "homehub-claude-refresh@claude2.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "CLAUDE_CONFIG_DIR=%s/.claude-2" % home in claude2
    gemini = (box.units / "homehub-gemini-refresh.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "ReadWritePaths=-%s/.gemini" % home in gemini
    # Ownership of the always-created Codex home goes to the effective account.
    owners = [c for c in box.calls if c.startswith("install ") and "homehub-ai-codex" in c]
    assert owners and all("-o alice -g alice" in c for c in owners), owners


@pytest.mark.parametrize("name", ["hub", "root"])
def test_hub_and_root_are_still_refused_llr1113(tmp_path, name):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_USER_ACCOUNT=" + name])
    assert box.run(ENSURE, name).returncode == 2
    assert box.run(SETUP_USAGE).returncode == 2
    box2 = Box(tmp_path / "b", ["AI_CLI_ENABLED=true", "AI_CLI_USER=" + name]) \
        if (tmp_path / "b").mkdir() is None else None
    assert box2.run(SETUP_CLI).returncode == 2
    assert box.accounts == [] and box2.accounts == []


def test_a_privileged_group_is_refused_and_nothing_is_created_llr1113(tmp_path):
    box = Box(tmp_path, [], accounts=["alice"])
    (box.root / "groups.alice").write_text("alice docker\n", encoding="utf-8")
    done = box.run(ENSURE, "alice")
    assert done.returncode == 2 and "docker" in done.stderr


def test_an_absent_account_is_created_not_refused_llr1113(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude"])
    assert box.accounts == []
    done = box.run(SETUP_USAGE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.accounts == ["homehub-ai"]


def test_neither_consumer_enabled_creates_nothing_llr1113(tmp_path):
    box = Box(tmp_path, ["AI_CLI_ENABLED=false", "AI_USAGE_ENABLED=false"])
    for script in (SETUP_CLI, SETUP_USAGE):
        done = box.run(script)
        assert done.returncode == 0, done.stdout + done.stderr
    assert box.accounts == [] and not [c for c in box.calls if c.startswith(("useradd", "install"))]


# ═══════════════════════════════════════════════════════════════════════════
# TC-1206 / TC-1207 - the Codex home (LLR-1114)
# ═══════════════════════════════════════════════════════════════════════════

def test_usage_without_codex_still_creates_the_codex_home_the_unit_lists_llr1114(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude,gemini"])
    done = box.run(SETUP_USAGE)
    assert done.returncode == 0, done.stdout + done.stderr
    # The path comes from the REAL unit file's ReadWritePaths=, not from here.
    unit = (USAGE_DIR / "homehub-ai-usage.service").read_text(encoding="utf-8")
    listed = re.findall(r"^ReadWritePaths=(.+)$", unit, re.M)
    assert listed, "the unit lists no ReadWritePaths"
    for path in " ".join(listed).split():
        made = [c for c in box.calls if c.startswith("install ") and c.endswith(" " + path)]
        assert made, "the unit lists %s but setup never created it: %r" % (path, box.calls)
        assert "-d" in made[0] and "-m 0700" in made[0] and "-o homehub-ai -g homehub-ai" in made[0], made[0]


def test_cli_only_references_no_codex_home_and_installs_no_usage_unit_llr1114(tmp_path):
    box = Box(tmp_path, ["AI_CLI_ENABLED=true", "AI_CLI_USER=alice"])
    done = box.run(SETUP_CLI)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "homehub-ai-codex" not in all_text(done) + tree_text(box.units) + "\n".join(box.calls)
    assert sorted(p.name for p in box.units.iterdir() if p.is_file()) == ["homehub-ai-cli.service"]


# ═══════════════════════════════════════════════════════════════════════════
# TC-1207 - Codex status per active consumer (LLR-1115)
# ═══════════════════════════════════════════════════════════════════════════

def _real_openai_enabled():
    """Independently derive today's answer from the shipped routes + registry."""
    ids = [l.split("#")[0].strip() for l in (CLI_DIR / "routes-enabled").read_text(encoding="utf-8").splitlines()]
    ids = [i for i in ids if i]
    rows = [l for l in (CLI_DIR / "agents.registry.csv").read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]
    fam = {r["Id"]: r["Family"] for r in csv.DictReader(rows)}
    return any(fam.get(i) == "OPENAI" for i in ids)


def _status(box, **extra):
    done = box.run(STATUS, **extra)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_usage_only_codex_shows_one_home_llr1115(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=codex", "AI_USAGE_USER_ACCOUNT=alice"])
    out = _status(box)
    assert "login codex (usage)" in out and "(ai-cli)" not in out
    assert "CODEX_HOME=" + box.codex_home.as_posix() in out


def test_ai_cli_codex_line_appears_only_with_an_openai_route_llr1115(tmp_path):
    box = Box(tmp_path, ["AI_CLI_ENABLED=true", "AI_CLI_USER=alice", "AI_USAGE_ENABLED=false"])
    # Today's shipped routes: the answer is whatever the registry says it is.
    out = _status(box)
    assert ("login codex (ai-cli)" in out) == _real_openai_enabled()
    # An OPENAI row enabled in a copy of the routes file adds exactly that line.
    routes = tmp_path / "routes"
    routes.write_text("ANALYSIS-QUICK\nANALYSIS-CODEX\n", encoding="utf-8")
    out = _status(box, AI_CLI_ROUTES_FILE=routes.as_posix())
    assert "login codex (ai-cli)" in out
    assert "login codex (usage)" not in out
    # Only anthropic rows: no codex line.
    routes.write_text("ANALYSIS-QUICK\n", encoding="utf-8")
    assert "login codex" not in _status(box, AI_CLI_ROUTES_FILE=routes.as_posix())


# ═══════════════════════════════════════════════════════════════════════════
# TC-1208 - credential paths (LLR-1116)
# ═══════════════════════════════════════════════════════════════════════════

MAPPINGS = {   # source -> (knob, path under <home>)   - the sign-in locations
    "claude": ("AI_USAGE_CLAUDE_CREDENTIALS", ".claude/.credentials.json"),
    "claude2": ("AI_USAGE_CLAUDE2_CREDENTIALS", ".claude-2/.credentials.json"),
    "gemini": ("AI_USAGE_GEMINI_CREDENTIALS", ".gemini/oauth_creds.json"),
    "opencode": ("AI_USAGE_OPENCODE_AUTH", ".local/share/opencode/auth.json"),
}


def _usage_box(tmp_path, source, knob_value, account="alice"):
    knob = MAPPINGS[source][0]
    return Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=" + source,
                                      "AI_USAGE_USER_ACCOUNT=" + account,
                                      "%s=%s" % (knob, knob_value)])


@pytest.mark.parametrize("source", sorted(MAPPINGS))
def test_each_supported_mapping_is_accepted_llr1116(tmp_path, source):
    knob, rel = MAPPINGS[source]
    box = _usage_box(tmp_path, source, "PLACEHOLDER")
    good = (box.home / "alice" / rel).as_posix()
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8").replace("PLACEHOLDER", good),
                            encoding="utf-8")
    out = _status(box)
    assert "unsupported-path" not in out


@pytest.mark.parametrize("source", sorted(MAPPINGS))
@pytest.mark.parametrize("bad", ["/etc/elsewhere/cred.json", ""])
def test_any_other_value_is_unsupported_with_the_exact_default_llr1116(tmp_path, source, bad):
    knob, rel = MAPPINGS[source]
    box = _usage_box(tmp_path, source, bad)
    out = _status(box)
    default = (box.home / "alice" / rel).as_posix()
    assert "unsupported-path" in out
    assert "set %s=%s" % (knob, default) in out
    # The feeder treats blank as UNSET; advice must never suggest it.
    assert "blank" not in out.lower() and "leave it" not in out.lower()


def test_env_example_defaults_resolve_through_the_feeders_own_reader_llr1116(tmp_path, monkeypatch):
    """The OTHER side: with the shipped defaults, the feeder's readers ask for
    exactly the files sign-in writes (and the status step accepts them)."""
    env = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Z0-9_]+=", line):
            key, _, value = line.partition("=")
            env[key] = value
    seen = {}

    class Stop(Exception):
        pass

    def spy(path, extractor):
        seen["path"] = path
        raise Stop()

    monkeypatch.setattr(feeder, "read_secret_file", spy)
    readers = {"claude": feeder.read_claude, "claude2": feeder.read_claude2,
               "gemini": feeder.read_gemini, "opencode": feeder.read_opencode}
    resolved = {}
    for source, reader in readers.items():
        seen.clear()
        with pytest.raises(Stop):
            reader(dict(env))
        resolved[source] = seen["path"]
    for source, (knob, rel) in MAPPINGS.items():
        assert resolved[source] == "/home/homehub-ai/" + rel, (source, resolved[source])
    # And the status step, given the example file's own values, accepts them all.
    box = Box(tmp_path, [])
    box.env_file.write_text("\n".join(
        ["AI_USAGE_ENABLED=true", "AI_USAGE_SOURCES=claude,claude2,gemini,opencode"]
        + ["%s=%s" % (k, env[k]) for k, _ in MAPPINGS.values()]) + "\n", encoding="utf-8")
    out = _status(box, STUB_HOME_BASE="/home")
    assert "unsupported-path" not in out, out


# ═══════════════════════════════════════════════════════════════════════════
# TC-1209 - the login matrix (LLR-1117)
# ═══════════════════════════════════════════════════════════════════════════

def _claude_box(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    cred = (box.home / "alice" / ".claude" / ".credentials.json").as_posix()
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8")
                            + "AI_USAGE_CLAUDE_CREDENTIALS=%s\n" % cred, encoding="utf-8")
    return box, cred


def test_login_owed_prints_the_recipe_with_the_wrapper_path_llr1117(tmp_path):
    box, _ = _claude_box(tmp_path)
    out = _status(box)
    assert "login claude: login owed" in out
    assert "LOGINS OWED: claude (login owed)" in out
    assert "sudo -u alice env HOME=%s" % (box.home / "alice").as_posix() in out
    assert "/usr/local/bin/claude auth login --claudeai" in out
    assert "PATH=" + PATH_STRING in out


def test_present_credential_is_not_verified_until_the_feeder_logged_live_llr1117(tmp_path):
    box, _ = _claude_box(tmp_path)
    box.cred("alice", ".claude/.credentials.json")
    out = _status(box)
    assert "login claude: credential present (not verified)" in out
    assert "installed, operation not yet verified" in out and "LOGINS OWED: none" in out
    assert "auth login" not in out
    required = [g.key for g in feeder.GAUGE_SPECS if g.source == "claude" and not g.optional]
    optional = [g.key for g in feeder.GAUGE_SPECS if g.source == "claude" and g.optional]
    assert required and optional, "the feeder defines required and optional claude gauges"
    live = [(k, "live") for k in required]
    box.journal({"gauges": live})
    out = _status(box)
    assert "login claude: verified" in out and "consumer operation verified" in out
    NOT = "login claude: credential present (not verified)"
    # One REQUIRED gauge UNAVAILABLE in the latest completed run.
    box.journal({"gauges": [(required[0], "UNAVAILABLE")] + live[1:]})
    assert NOT in _status(box)
    # SOL: an old good run followed by a PARTIAL new one (a required gauge never printed).
    box.journal({"gauges": live, "age": 600}, {"gauges": live[:1], "age": 60})
    assert NOT in _status(box)
    # SOL: a new invocation that FAILS EARLY (no cycle-end line, nothing useful) does not
    # borrow the older good run's success.
    box.journal({"gauges": live, "age": 600},
                {"gauges": [], "complete": False, "extra": ["Traceback (most recent call last):"], "age": 60})
    assert NOT in _status(box)
    # ...but a run that is still going right now leaves the previous completed run as
    # the latest finished evidence.
    box.journal({"gauges": live, "age": 600}, {"gauges": live[:1], "complete": False, "age": 5, "id": "runC"})
    assert NOT in _status(box)                                   # service not running: it died
    assert "login claude: verified" in _status(
        box, AI_CLI_USAGE_SERVICE_STATE="activating", AI_CLI_USAGE_INVOCATION_ID="runC")
    # A stale OPTIONAL gauge live, then the required ones UNAVAILABLE: not proof.
    box.journal({"gauges": [(optional[0], "live")] + [(k, "UNAVAILABLE") for k in required]})
    assert NOT in _status(box)
    # Evidence past the age cap is dropped (default 20 minutes).
    box.journal({"gauges": live, "age": 3600})
    assert NOT in _status(box)
    box.journal({"gauges": live, "age": 600})
    assert "login claude: verified" in _status(box)
    # A journal with no cycle-end line (an older feeder) is never "verified".
    box.journal({"gauges": live, "complete": False})
    assert NOT in _status(box)


def test_codex_verifies_through_its_bare_gauge_id_llr1117(tmp_path):
    codex_ids = [g.key for g in feeder.GAUGE_SPECS if g.source == "codex" and not g.optional]
    assert codex_ids == ["ai-usage-codex"]      # the BARE id the feeder posts
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=codex", "AI_USAGE_USER_ACCOUNT=alice"])
    path = box.codex_home / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(CANARY, encoding="utf-8")
    assert "login codex (usage): credential present (not verified)" in _status(box)
    box.journal({"gauges": [(k, "live") for k in codex_ids]})
    assert "login codex (usage): verified" in _status(box)


def test_account_that_cannot_be_acted_as_is_cannot_check_not_login_owed_llr1117(tmp_path):
    box, _ = _claude_box(tmp_path)
    out = _status(box, AI_CLI_RUNAS="no-such-runuser-command")       # rc 127
    assert "login claude: cannot check" in out
    assert "LOGINS OWED: unknown" in out
    assert "auth login" not in out and "login owed" not in out


def test_empty_credential_is_login_owed_unreadable_is_permissions_llr1117(tmp_path):
    box, cred = _claude_box(tmp_path)
    box.cred("alice", ".claude/.credentials.json", "")
    out = _status(box)
    assert "login claude: login owed" in out and "auth login" in out
    box.cred("alice", ".claude/.credentials.json", CANARY)
    (box.root / "deny").write_text(cred + "\n", encoding="utf-8")
    assert "login claude: permissions wrong" in _status(box)


def test_root_owned_0600_for_a_non_root_account_is_permissions_wrong_llr1117(tmp_path):
    box, cred = _claude_box(tmp_path)
    box.cred("alice", ".claude/.credentials.json")
    (box.root / "deny").write_text(cred + "\n", encoding="utf-8")   # the account cannot read it
    out = _status(box)
    assert "login claude: permissions wrong" in out
    assert "sudo chown alice:alice %s && sudo chmod 600 %s" % (cred, cred) in out
    assert "root:root" in out and "600" in out
    assert "auth login" not in out, "a permissions problem must not offer a login recipe"


@pytest.mark.parametrize("settings,expect,recipe", [
    (None, "auth type not selected", True),
    ({"security": {"auth": {"selectedType": "gemini-api-key"}}}, "wrong auth type gemini-api-key", True),
    ({"security": {"auth": {"selectedType": "oauth-personal"}}}, "credential present (not verified)", False),
])
def test_gemini_needs_the_oauth_personal_auth_type_llr1117(tmp_path, settings, expect, recipe):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=gemini", "AI_USAGE_USER_ACCOUNT=alice"])
    cred = box.cred("alice", ".gemini/oauth_creds.json").as_posix()
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8")
                            + "AI_USAGE_GEMINI_CREDENTIALS=%s\n" % cred, encoding="utf-8")
    if settings is not None:
        box.cred("alice", ".gemini/settings.json", json.dumps(settings))
    out = _status(box)
    assert "login gemini: " + expect in out
    assert ("NO_BROWSER=true" in out) == recipe
    if recipe:
        assert 'sign in with "Login with Google"' in out


def test_no_credential_content_ever_reaches_the_output_llr1117(tmp_path):
    box, cred = _claude_box(tmp_path)
    box.cred("alice", ".claude/.credentials.json", '{"accessToken": "%s"}' % CANARY)
    outputs = [_status(box)]
    (box.root / "deny").write_text(cred + "\n", encoding="utf-8")
    outputs.append(_status(box))
    assert CANARY not in "".join(outputs)


# ═══════════════════════════════════════════════════════════════════════════
# TC-1210 - two levels of status (LLR-1118)
# ═══════════════════════════════════════════════════════════════════════════

def test_pending_cli_is_provisioning_not_an_error_llr1118(tmp_path):
    status = json.loads(json.dumps(ALL_INSTALLED))
    status["provisioning"] = "incomplete"
    status["clis"]["claude"].update(installed=False, version=None, pending_reason="download pending")
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"], status=status)
    out = _status(box)
    assert out.splitlines()[0].startswith("PROVISIONING: INCOMPLETE")
    assert "claude: being provisioned (download pending)" in out
    assert not UNHAPPY.search(out), out
    # An unreadable provisioner is a calm sentence too, and the script still exits 0.
    out = _status(box, AI_CLI_PROVISIONER=(tmp_path / "absent.sh").as_posix())
    assert out.startswith("PROVISIONING: status unavailable") and not UNHAPPY.search(out)


def test_status_orders_its_summary_lines_llr1118(tmp_path):
    box, _ = _claude_box(tmp_path)
    lines = _status(box).splitlines()
    assert lines[0].startswith("PROVISIONING:")
    assert [i for i, l in enumerate(lines) if l.startswith("LOGINS OWED:")][0] > 0


# ═══════════════════════════════════════════════════════════════════════════
# TC-1211 - the firstboot hook (LLR-1119)
# ═══════════════════════════════════════════════════════════════════════════

def _hook_text():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    start = text.index("ai_cli_provision_hook() {")
    end = text.index("\n}\n", start) + 3
    # the shared unquote helper the hook (and 6d/6e) call is a top-level function
    u0 = text.index("ai_unq() {")
    unq = text[u0:text.index("\n}\n", u0) + 3]
    return text, start, unq + text[start:end]


def test_firstboot_hook_only_schedules_and_runs_before_6d_llr1119():
    text, start, hook = _hook_text()
    assert "--schedule" in hook
    code = "\n".join(l for l in hook.splitlines() if not l.lstrip().startswith("#"))
    assert "--run" not in code, "firstboot must never wait on a download"
    assert hook.index("ensure-ai-account.sh\" \"$AI_EFFECTIVE_ACCOUNT\"") < hook.index("provision-ai-clis.sh\" --schedule")
    call = text.index("\nai_cli_provision_hook ||")
    assert call > start
    assert call < text.index("# ── 6d. the AI CLI service")
    assert call < text.index("# ── 6e. the AI-usage feeder")
    # The status summary comes after 6e.
    assert text.index("ai-cli-status.sh", text.index("# ── 6e.")) > text.index("# ── 6e.")


def _run_hook(box, tmp_path, env_lines):
    stack = tmp_path / "stack-dir"
    (stack / "ai-cli").mkdir(parents=True)
    for name in ("ensure-ai-account.sh", "ai_cli_service.py"):
        shutil.copy(str(CLI_DIR / name), str(stack / "ai-cli" / name))
    shutil.copy(str(box.root / "provision-ai-clis.sh"), str(stack / "ai-cli" / "provision-ai-clis.sh"))
    for unit in ("homehub-ai-clis-provision.service", "homehub-ai-clis-provision.timer"):
        (stack / "ai-cli" / unit).write_text("[Unit]\n# stand-in\n", encoding="utf-8")
    (stack / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    _, _, hook = _hook_text()
    script = tmp_path / "hook.sh"
    script.write_text(
        "set -euo pipefail\n"
        "STACK_DIR=%s\n"
        "log() { echo \"[firstboot] $*\"; }\n"
        "_env_val() { sed -n \"s/^$1=//p\" \"$STACK_DIR/.env\" 2>/dev/null | head -1 | tr -d '\\r'; }\n"
        "AI_EFFECTIVE_ACCOUNT=\"\"\n"
        "%s\nai_cli_provision_hook\n" % (stack.as_posix(), hook), encoding="utf-8")
    return subprocess.run([box.bash, script.as_posix()],
                          env=box.env(AI_CLI_UNIT_DIR=box.units.as_posix()),
                          capture_output=True, text=True)


def test_extracted_hook_creates_the_account_then_schedules_never_runs_llr1119(tmp_path):
    box = Box(tmp_path, [])
    done = _run_hook(box, tmp_path, ["AI_USAGE_ENABLED=true", "AI_USAGE_USER_ACCOUNT=bob",
                                    "AI_CLI_ENABLED=true", "AI_CLI_USER=alice"])
    assert done.returncode == 0, done.stdout + done.stderr
    calls = box.calls
    assert box.accounts == ["alice"]
    assert "provisioner --schedule" in calls and "provisioner --run" not in calls
    assert calls.index("useradd alice") < calls.index("provisioner --schedule")
    # --schedule installs the provisioning units itself; the hook must not.
    assert not list(box.units.iterdir())
    assert "bob" not in done.stdout


def test_extracted_hook_does_nothing_when_no_consumer_is_enabled_llr1119(tmp_path):
    box = Box(tmp_path, [])
    done = _run_hook(box, tmp_path, ["AI_USAGE_ENABLED=false", "AI_CLI_ENABLED=false"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.calls == [] and box.accounts == []


def test_firstboot_parses_and_the_config_validator_passes_llr1119():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    assert subprocess.run([bash, "-n", FIRSTBOOT.as_posix()], capture_output=True).returncode == 0
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "validate_config.py")],
                          capture_output=True, text=True, cwd=str(REPO))
    assert done.returncode == 0, done.stdout[-800:]


@pytest.mark.parametrize("script", [ENSURE, STATUS, UPDATER, SETUP_CLI, SETUP_USAGE])
def test_edited_scripts_parse_and_are_lf_llr1119(script):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    assert subprocess.run([bash, "-n", script.as_posix()], capture_output=True).returncode == 0
    assert b"\r" not in script.read_bytes()


# ═══════════════════════════════════════════════════════════════════════════
# TC-1212 - the /etc updater files and the unit PATH strings (LLR-1120)
# ═══════════════════════════════════════════════════════════════════════════

def _run_updater(tmp_path, *args):
    bash = _bash(tmp_path)
    etc = tmp_path / "etc"
    done = subprocess.run([bash, UPDATER.as_posix()] + list(args),
                          env=dict(os.environ, AI_CLI_ETC_DIR=etc.as_posix()),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return etc


def test_updater_files_have_exactly_the_documented_keys_llr1120(tmp_path):
    etc = _run_updater(tmp_path, "--gemini")
    claude = json.loads((etc / "claude-code" / "managed-settings.json").read_text(encoding="utf-8"))
    gemini = json.loads((etc / "gemini-cli" / "settings.json").read_text(encoding="utf-8"))
    assert claude == {"env": {"DISABLE_AUTOUPDATER": "1"}}
    assert gemini == {"general": {"enableAutoUpdate": False}}


def test_gemini_file_only_when_gemini_is_a_source_and_existing_keys_survive_llr1120(tmp_path):
    etc = tmp_path / "etc"
    (etc / "claude-code").mkdir(parents=True)
    (etc / "claude-code" / "managed-settings.json").write_text(
        json.dumps({"env": {"KEEP": "me"}, "other": 1}), encoding="utf-8")
    _run_updater(tmp_path)
    assert not (etc / "gemini-cli").exists()
    merged = json.loads((etc / "claude-code" / "managed-settings.json").read_text(encoding="utf-8"))
    assert merged == {"env": {"KEEP": "me", "DISABLE_AUTOUPDATER": "1"}, "other": 1}
    before = (etc / "claude-code" / "managed-settings.json").stat().st_mtime_ns
    _run_updater(tmp_path)
    assert (etc / "claude-code" / "managed-settings.json").stat().st_mtime_ns == before


def test_setup_scripts_install_the_updater_files_llr1120(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude,gemini"])
    assert box.run(SETUP_USAGE).returncode == 0
    etc = tmp_path                       # dirname(dirname(units)) is the test dir
    assert json.loads((etc / "claude-code" / "managed-settings.json").read_text(encoding="utf-8")) \
        == {"env": {"DISABLE_AUTOUPDATER": "1"}}
    assert (etc / "gemini-cli" / "settings.json").exists()


def test_every_shipped_unit_keeps_the_pinned_path_llr1120():
    units = sorted(list(CLI_DIR.glob("*.service")) + list(USAGE_DIR.glob("*.service")))
    assert len(units) >= 3
    carriers = 0
    for unit in units:
        for line in unit.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^Environment=PATH=(.*)$", line.strip())
            if m:
                carriers += 1
                assert m.group(1) == PATH_STRING == service.DEFAULT_BIN_PATH, (unit.name, m.group(1))
    assert carriers >= 2


# ═══════════════════════════════════════════════════════════════════════════
# TC-1213 - .env.example (LLR-1120)
# ═══════════════════════════════════════════════════════════════════════════

def test_env_example_knobs_appear_exactly_once_with_their_defaults_llr1120():
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()

    def values(key):
        return [l.partition("=")[2] for l in lines if l.startswith(key + "=")]

    assert values("AI_CLI_PROVISION") == ["auto"]
    assert values("AI_CLI_PROVISION_RETRY_MINUTES") == ["15"]
    for source, (knob, rel) in MAPPINGS.items():
        assert values(knob) == ["/home/homehub-ai/" + rel], knob


# ═══════════════════════════════════════════════════════════════════════════
# TC-1214 - the happy path says nothing alarming (LLR-1118)
# ═══════════════════════════════════════════════════════════════════════════

def test_happy_path_output_has_no_missing_not_found_or_refus_llr1118(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude,codex", "AI_USAGE_USER_ACCOUNT=alice",
                                     "AI_CLI_ENABLED=true", "AI_CLI_USER=alice"], accounts=[])
    cred = box.cred("alice", ".claude/.credentials.json").as_posix()
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8")
                            + "AI_USAGE_CLAUDE_CREDENTIALS=%s\n" % cred, encoding="utf-8")
    outputs = [box.run(ENSURE, "alice"), box.run(SETUP_USAGE), box.run(SETUP_CLI), box.run(STATUS)]
    for done in outputs:
        assert done.returncode == 0, done.stdout + done.stderr
    text = all_text(*outputs)
    assert not UNHAPPY.search(text), UNHAPPY.search(text).group(0) + "\n" + text


# ═══════════════════════════════════════════════════════════════════════════
# The real contract (needs W1's provisioner in the tree)
# ═══════════════════════════════════════════════════════════════════════════

def _real_seams(box, tmp_path):
    """Point W1's real provisioner at a throwaway prefix/state, never the box."""
    (tmp_path / "prefix").mkdir(exist_ok=True)
    return {"AI_CLIS_PREFIX": (tmp_path / "prefix").as_posix(),
            "AI_CLIS_STATE_DIR": (tmp_path / "state").as_posix(),
            "AI_CLIS_BIN_DIR": (tmp_path / "bin").as_posix(),
            "AI_CLIS_LOCK": (tmp_path / "lock").as_posix(),
            "AI_CLIS_ENV_FILE": box.env_file.as_posix()}


def test_real_provisioner_contract(tmp_path):
    """W1's real `--status --json` against this repo's status step. MUST RUN."""
    provisioner = CLI_DIR / "provision-ai-clis.sh"
    assert provisioner.exists(), "W1's provisioner is in the tree after the rebase"
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    seams = _real_seams(box, tmp_path)
    done = subprocess.run([box.bash, provisioner.as_posix(), "--status", "--json"],
                          env=box.env(**seams), capture_output=True, text=True)
    shape = json.loads(done.stdout)
    assert shape["provisioning"] == "incomplete", shape       # claude is needed and not installed
    assert set(shape["clis"]) >= {"claude", "codex", "opencode", "gemini", "node"}
    one = shape["clis"]["claude"]
    assert set(one) == {"installed", "identity", "version", "link_target", "activation_state", "pending_reason"}
    assert one["installed"] is False
    out = _status(box, AI_CLI_PROVISIONER=provisioner.as_posix(), **seams)
    lines = out.splitlines()
    assert lines[0].startswith("PROVISIONING: INCOMPLETE"), out
    assert "claude: being provisioned" in out, out
    assert not UNHAPPY.search(out.split("claude: being provisioned")[0]), out
    # With provisioning switched off the real tool says so and the step relays it.
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8") + "AI_CLI_PROVISION=off\n", encoding="utf-8")
    off = _status(box, AI_CLI_PROVISIONER=provisioner.as_posix(), **seams)
    assert off.startswith("PROVISIONING: off"), off


REAL_HOOK = r"""set -u
U=w2hooktest
W=$(mktemp -d)
cleanup() { userdel -r $U >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
mkdir -p $W/stack/ai-cli $W/units $W/state $W/prefix $W/bin
cp -r @@REPO@@/stack/ai-cli/. $W/stack/ai-cli/
rm -rf $W/stack/ai-cli/tests $W/stack/ai-cli/__pycache__
printf 'AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=claude\nAI_USAGE_USER_ACCOUNT=%s\nAI_CLI_ENABLED=false\n' $U > $W/stack/.env
cat > $W/systemctl <<'EOS'
#!/usr/bin/env bash
echo "systemctl $*" >> "$SYSLOG"
exit 0
EOS
chmod +x $W/systemctl
export SYSLOG=$W/calls.log AI_CLIS_SYSTEMCTL=$W/systemctl AI_CLI_UNIT_DIR=$W/units AI_CLIS_UNIT_DIR=$W/units
export AI_CLIS_PREFIX=$W/prefix AI_CLIS_STATE_DIR=$W/state AI_CLIS_BIN_DIR=$W/bin AI_CLIS_LOCK=$W/lock AI_CLIS_ENV_FILE=$W/stack/.env
set -e
STACK_DIR=$W/stack
log() { echo "[firstboot] $*"; }
_env_val() { sed -n "s/^$1=//p" "$STACK_DIR/.env" 2>/dev/null | head -1 | tr -d '\r'; }
AI_EFFECTIVE_ACCOUNT=""
. @@DIR@@/hook-fn.sh
ai_cli_provision_hook
echo "HOOK-RC=$?"
ls $W/units
cat $W/calls.log
"""


def test_firstboot_hook_with_the_real_provisioner_schedules_and_installs_the_units_llr1119(tmp_path):
    """Real Linux, real root, W1's real --schedule: it installs the units itself,
    enables the timer BEFORE starting the service with --no-block, and the hook
    never calls --run."""
    _, _, hook = _hook_text()
    with open(str(tmp_path / "hook-fn.sh"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(hook)
    done = _wsl_root(REAL_HOOK, tmp_path)
    out = done.stdout
    assert done.returncode == 0, done.stdout + done.stderr
    assert "HOOK-RC=0" in out and "AI CLI provisioning scheduled" in out, out
    assert "homehub-ai-clis-provision.service" in out and "homehub-ai-clis-provision.timer" in out
    calls = [l for l in out.splitlines() if l.startswith("systemctl ")]
    enable = [i for i, c in enumerate(calls) if " enable" in c and "provision.timer" in c]
    start = [i for i, c in enumerate(calls) if " start" in c and "--no-block" in c]
    assert enable and start and enable[0] < start[0], calls
    assert not [c for c in calls if " --run" in c]


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 - the legacy refresher can never race the new one (BLOCKER)
# ═══════════════════════════════════════════════════════════════════════════

LEGACY = "homehub-claude-refresh"


def _legacy_box(tmp_path, sources="claude"):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=" + sources, "AI_USAGE_USER_ACCOUNT=alice"],
              accounts=["alice"])
    (box.units / (LEGACY + ".service")).write_text("[Service]\n# OLD\n", encoding="utf-8")
    (box.units / (LEGACY + ".timer")).write_text("[Timer]\n# OLD\n", encoding="utf-8")
    (box.root / "enabled").write_text(LEGACY + ".timer\n", encoding="utf-8")
    return box


OLD_TIMER = LEGACY + ".timer"
NEW_TIMER = LEGACY + "@claude.timer"


def _enabled(box):
    return (box.root / "enabled").read_text(encoding="utf-8").split()


def _refreshers(box):
    return [u for u in _enabled(box) if u in (OLD_TIMER, NEW_TIMER)]


def test_failed_legacy_disable_keeps_the_old_refresher_and_never_enables_the_new_one_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="disable --now %s.timer" % LEGACY)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "WARNING: could not disable %s.timer" % LEGACY in done.stdout
    assert "systemctl enable --now homehub-claude-refresh@claude.timer" not in box.calls
    # The old pair is still the refresher; nothing was removed or stopped.
    assert (box.units / (LEGACY + ".timer")).exists() and (box.units / (LEGACY + ".service")).exists()
    assert not [c for c in box.calls if c.startswith("systemctl stop")]
    # The rest of the install still converged (the other timers are not held back).
    assert "systemctl enable --now homehub-ai-usage.timer" in box.calls


def test_busy_legacy_service_at_the_deadline_defers_the_migration_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, STUB_ACTIVE="activating")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "was still activating" in done.stdout and "DEFERRED" in done.stdout
    assert "systemctl enable --now homehub-claude-refresh@claude.timer" not in box.calls
    assert (box.units / (LEGACY + ".service")).exists()
    assert not [c for c in box.calls if c.startswith("systemctl stop")]


def test_an_idle_legacy_service_still_migrates_and_exits_0_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (box.units / (LEGACY + ".service")).exists()
    assert "systemctl enable --now homehub-claude-refresh@claude.timer" in box.calls


def test_firstboot_keeps_going_when_the_usage_setup_exits_3_sr067():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    block = text[text.index("# ── 6e."):text.index("# ── 6f.")]
    assert 'bash "$STACK_DIR/ai-usage/setup-ai-usage.sh"' in block and "if AI_CLI_SKIP_STATUS=1 bash" in block         and "else" in block, \
        "a non-zero exit takes the WARNING branch, never aborts firstboot"


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 - existing state directories are never chowned (MAJOR)
# ═══════════════════════════════════════════════════════════════════════════

def test_existing_state_directories_of_another_account_are_left_alone_and_reported_llr1114(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude2,gemini", "AI_USAGE_USER_ACCOUNT=alice"],
              accounts=["alice"])
    kept = {}
    for rel in (".claude-2", ".gemini"):
        d = box.home / "alice" / rel
        d.mkdir(parents=True)
        (d / "credential").write_text(CANARY, encoding="utf-8")
        kept[rel] = d
    # An msys-style home (no drive colon) so the script's own `cut -d:` can split it.
    base = box.home.as_posix()
    base = "/%s%s" % (base[0].lower(), base[2:]) if base[1:2] == ":" else base
    done = box.run(SETUP_USAGE, STUB_STAT_OWNER="bob", STUB_HOME_BASE=base)
    assert done.returncode == 0, done.stdout + done.stderr
    for rel, d in kept.items():
        mutating = [c for c in box.calls if c.startswith("install ") and c.endswith("/home/alice/" + rel)]
        assert not mutating, "an existing %s must not be install -d'd: %r" % (rel, mutating)
        assert "/home/alice/%s already exists and belongs to bob" % rel in done.stdout
        assert (d / "credential").read_text(encoding="utf-8") == CANARY


def test_a_rerun_leaves_credentials_and_state_byte_identical_and_a_fresh_dir_is_created_llr1114(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude,claude2,gemini", "AI_USAGE_USER_ACCOUNT=alice"])
    first = box.run(SETUP_USAGE)
    assert first.returncode == 0, first.stdout + first.stderr
    assert [c for c in box.calls if c.startswith("install -d -m 0700 -o alice -g alice")
            and c.endswith("/.gemini")], "an ABSENT directory is created for the account"
    files = {}
    for rel, body in ((".claude/.credentials.json", CANARY), (".claude-2/.credentials.json", CANARY + "2"),
                      (".gemini/oauth_creds.json", CANARY + "3"), (".gemini/settings.json", "{}")):
        files[box.cred("alice", rel, body)] = body.encode("utf-8")
    box.codex_home.mkdir(parents=True)
    codex = box.codex_home / "auth.json"
    codex.write_text(CANARY + "4", encoding="utf-8")
    files[codex] = (CANARY + "4").encode("utf-8")
    stamps = {p: p.stat().st_mtime_ns for p in files}
    second = box.run(SETUP_USAGE)
    assert second.returncode == 0, second.stdout + second.stderr
    for path, body in files.items():
        assert path.read_bytes() == body and path.stat().st_mtime_ns == stamps[path], path


@pytest.mark.parametrize("script", [SETUP_USAGE, SETUP_CLI], ids=["usage", "cli"])
def test_both_setups_keep_the_other_keys_of_an_existing_updater_file_llr1120(tmp_path, script):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice",
                                     "AI_CLI_ENABLED=true", "AI_CLI_USER=alice"], accounts=["alice"])
    target = tmp_path / "claude-code" / "managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"env": {"KEEP": "1"}, "permissions": {"deny": ["x"]}}), encoding="utf-8")
    done = box.run(script)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "env": {"KEEP": "1", "DISABLE_AUTOUPDATER": "1"}, "permissions": {"deny": ["x"]}}


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 - the updater merge never destroys or broadens (MAJOR)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("seed,gemini", [
    ({"env": ["owner-setting"]}, False),
    ({"env": "a string"}, False),
    ({"general": ["x"]}, True),
])
def test_a_non_object_branch_is_left_untouched_with_a_warning_llr1120(tmp_path, seed, gemini):
    etc = tmp_path / "etc"
    name = "gemini-cli/settings.json" if gemini else "claude-code/managed-settings.json"
    path = etc / name
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(seed), encoding="utf-8")
    before = path.read_bytes()
    args = ["--gemini"] if gemini else []
    done = subprocess.run([_bash(tmp_path), UPDATER.as_posix()] + args,
                          env=dict(os.environ, AI_CLI_ETC_DIR=etc.as_posix()), capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert path.read_bytes() == before
    assert "left as it is" in done.stdout
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".updater-")]


UPDATER_MODE = r"""set -eu
D=$(mktemp -d)/etc    # a real Linux filesystem: drvfs ignores chmod/chown
trap 'rm -rf "$(dirname $D)"' EXIT
mkdir -p $D/claude-code
printf '{"env":{"SECRET":"s"}}' > $D/claude-code/managed-settings.json
chown 12345:12345 $D/claude-code/managed-settings.json
chmod 0640 $D/claude-code/managed-settings.json
AI_CLI_ETC_DIR=$D bash @@REPO@@/stack/ai-cli/install-updater-controls.sh
stat -c '%a %u %g' $D/claude-code/managed-settings.json
ls -A $D/claude-code
cat $D/claude-code/managed-settings.json
"""


def test_updater_keeps_the_mode_and_owner_of_an_existing_file_llr1120(tmp_path):
    done = _wsl_root(UPDATER_MODE, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert "640 12345 12345" in lines, done.stdout
    assert ".updater-" not in done.stdout
    assert '"SECRET":"s"' in done.stdout and "DISABLE_AUTOUPDATER" in done.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 - firstboot warns, never aborts (MAJOR)
# ═══════════════════════════════════════════════════════════════════════════

def test_hook_survives_every_failing_step_under_set_e_llr1119(tmp_path):
    box = Box(tmp_path, [])
    _, _, hook = _hook_text()
    stack = tmp_path / "stack-dir"
    (stack / "ai-cli").mkdir(parents=True)
    (stack / ".env").write_text("AI_CLI_ENABLED=true\nAI_CLI_USER=alice\n", encoding="utf-8")
    script = tmp_path / "hook2.sh"
    script.write_text(
        "set -euo pipefail\nSTACK_DIR=%s\nlog() { echo \"[firstboot] $*\"; }\n"
        "_env_val() { sed -n \"s/^$1=//p\" \"$STACK_DIR/.env\" | head -1; }\n"
        "AI_EFFECTIVE_ACCOUNT=\"\"\n%s\nai_cli_provision_hook\necho AFTER\n" % (stack.as_posix(), hook),
        encoding="utf-8")
    # No ensure script, no provisioner on the payload at all.
    done = subprocess.run([box.bash, script.as_posix()], env=box.env(), capture_output=True, text=True)
    assert done.returncode == 0 and "AFTER" in done.stdout and "WARNING" in done.stdout, done.stdout + done.stderr
    # The ensure script is present but cannot resolve (exits non-zero): still a warning.
    (stack / "ai-cli" / "ensure-ai-account.sh").write_text("exit 9\n", encoding="utf-8")
    done = subprocess.run([box.bash, script.as_posix()], env=box.env(), capture_output=True, text=True)
    assert done.returncode == 0 and "AFTER" in done.stdout and "WARNING" in done.stdout, done.stdout + done.stderr


def test_firstboot_leftover_instructions_follow_the_effective_account_llr1119():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    six_d = text[text.index("# ── 6d."):text.index("# ── mesh VPN")]
    six_e = text[text.index("# ── 6e."):text.index("# ── 6f.")]
    assert "setup-token" not in six_d and "setup-token" not in six_e
    assert "AI_EFFECTIVE_ACCOUNT" in six_e
    assert "${AI_USAGE_USER_ACCOUNT:-homehub-ai}'s home" not in six_e


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 - REAL Linux, real root, real second user, no stubs (WSL only)
# ═══════════════════════════════════════════════════════════════════════════

def _wsl_root(script_text, tmp_path):
    wsl = shutil.which("wsl")
    if not wsl:
        pytest.skip("no WSL on this machine")
    probe = subprocess.run([wsl, "-d", "Ubuntu", "-u", "root", "--", "id", "-u"], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "0":
        pytest.skip("WSL Ubuntu is not available as root")

    def mnt(path):
        path = Path(path)
        return "/mnt/%s/%s" % (path.drive.rstrip(":").lower(), path.as_posix()[len(path.drive):].lstrip("/"))

    script = tmp_path / "real.sh"
    with open(str(script), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(script_text.replace("@@DIR@@", mnt(tmp_path)).replace("@@REPO@@", mnt(REPO)))
    return subprocess.run([wsl, "-d", "Ubuntu", "-u", "root", "--", "bash", mnt(script)],
                          capture_output=True, text=True)


REAL_STATUS = r"""set -u
U=w2realtest
useradd -m -s /bin/bash $U 2>/dev/null || true
trap 'userdel -r $U >/dev/null 2>&1' EXIT
H=$(getent passwd $U | cut -d: -f6)
chmod 755 $H; mkdir -p $H/.claude; chmod 755 $H/.claude
printf '{"token":"CANARY-REAL"}' > $H/.claude/.credentials.json
chown root:root $H/.claude/.credentials.json; chmod 600 $H/.claude/.credentials.json
printf 'AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=claude\nAI_USAGE_USER_ACCOUNT=%s\nAI_USAGE_CLAUDE_CREDENTIALS=%s/.claude/.credentials.json\n' $U $H > @@DIR@@/real.env
AI_CLI_ENV_FILE=@@DIR@@/real.env AI_CLI_PROVISIONER=/nonexistent bash @@REPO@@/stack/ai-cli/ai-cli-status.sh
"""


def test_real_root_owned_credential_for_a_real_second_user_is_permissions_wrong_llr1117(tmp_path):
    done = _wsl_root(REAL_STATUS, tmp_path)
    out = done.stdout
    assert done.returncode == 0, done.stdout + done.stderr
    assert "login claude: permissions wrong" in out, out
    assert "sudo chown w2realtest:w2realtest" in out
    assert "auth login" not in out, "a permissions problem must not offer a login recipe"
    assert "CANARY-REAL" not in out


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 2 - the INVARIANT: on every way out, exactly ONE account-1 refresh
# timer is enabled. (Round 1 left ZERO on a deferral.)
# ═══════════════════════════════════════════════════════════════════════════

BRANCHES = [
    # id, extra env, expected exit, expected surviving refresher
    ("idle", {}, 0, NEW_TIMER),
    ("busy-at-deadline", {"STUB_ACTIVE": "activating"}, 3, OLD_TIMER),
    ("disable-fails", {"STUB_SYSTEMCTL_FAIL": "disable --now " + OLD_TIMER}, 3, OLD_TIMER),
    ("stop-half-fails", {"STUB_SYSTEMCTL_FAIL": "disable --now " + OLD_TIMER,
                         "STUB_DISABLE_HALF": "1"}, 3, OLD_TIMER),
    ("new-timer-enable-fails", {"STUB_SYSTEMCTL_FAIL": "enable --now " + NEW_TIMER}, 3, OLD_TIMER),
    ("is-active-query-fails", {"STUB_SYSTEMCTL_FAIL": "is-active"}, 3, OLD_TIMER),
    ("is-active-unrecognised", {"STUB_ACTIVE": "mystery"}, 3, OLD_TIMER),
    ("enable-half-applied-but-running",
     {"STUB_SYSTEMCTL_FAIL": "enable --now " + NEW_TIMER, "STUB_ENABLE_HALF": "1"}, 0, NEW_TIMER),
    ("enable-ok-start-failed",
     {"STUB_SYSTEMCTL_FAIL": "enable --now " + NEW_TIMER, "STUB_ENABLE_NOSTART": "1"}, 3, OLD_TIMER),
    ("error-mid-migration", {"STUB_SYSTEMCTL_FAIL": "daemon-reload"}, None, OLD_TIMER),
]


@pytest.mark.parametrize("name,extra,code,survivor", BRANCHES, ids=[b[0] for b in BRANCHES])
def test_exactly_one_account_one_refresh_timer_survives_every_branch_sr067(tmp_path, name, extra, code, survivor):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, **extra)
    if code is None:
        assert done.returncode not in (0, 3), "the injected error must surface: " + done.stdout
    else:
        assert done.returncode == code, done.stdout + done.stderr
    assert _refreshers(box) == [survivor], (_enabled(box), done.stdout)
    # No timer may be left "enabled but not started": at the next boot it would run.
    leftover = (box.root / "enabledonly").read_text(encoding="utf-8").split()
    assert not [u for u in leftover if u in (OLD_TIMER, NEW_TIMER)], leftover
    assert not [c for c in box.calls if c.startswith("systemctl stop")]
    if survivor == OLD_TIMER:
        # The deferral is honest: it says the OLD timer is the refresher.
        assert (box.units / (LEGACY + ".timer")).exists()
    else:
        assert not (box.units / (LEGACY + ".timer")).exists()


def test_a_failed_restore_is_a_loud_error_naming_the_manual_command_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    # Busy at the deadline, AND the old timer cannot be re-enabled.
    done = box.run(SETUP_USAGE, STUB_ACTIVE="activating", STUB_SYSTEMCTL_FAIL="enable --now " + OLD_TIMER)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "ERROR: could NOT re-enable %s" % OLD_TIMER in done.stdout
    assert "systemctl enable --now %s" % OLD_TIMER in done.stdout
    assert _refreshers(box) == []          # the state the error message is about


def test_the_warning_never_claims_a_refresher_that_is_not_running_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, STUB_ACTIVE="activating")
    assert "is enabled again" in done.stdout and OLD_TIMER in _enabled(box)


def test_existing_codex_home_of_another_account_is_left_alone_llr1114(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=codex", "AI_USAGE_USER_ACCOUNT=alice"], accounts=["alice"])
    box.codex_home.mkdir(parents=True)
    auth = box.codex_home / "auth.json"
    auth.write_text(CANARY, encoding="utf-8")
    before = auth.stat().st_mtime_ns
    # AI_USAGE_CODEX_HOME is the path setup-ai-usage.sh itself creates/owns.
    done = box.run(SETUP_USAGE, STUB_STAT_OWNER="bob", AI_USAGE_CODEX_HOME=box.codex_home.as_posix())
    assert done.returncode == 0, done.stdout + done.stderr
    codex = [c for c in box.calls if c.startswith("install ") and c.endswith(box.codex_home.as_posix())]
    assert not codex, "an existing Codex home must not be install -d'd: %r" % codex
    assert "already exists and belongs to bob" in done.stdout
    assert auth.read_bytes() == CANARY.encode("utf-8") and auth.stat().st_mtime_ns == before


def test_the_feeders_cycle_end_line_is_the_one_the_status_step_looks_for_llr1117():
    """The OTHER side of the contract: what the feeder really prints."""
    feeder_src = (USAGE_DIR / "ai_usage_feeder.py").read_text(encoding="utf-8")
    printed = re.search(r'print\("(ai-usage: cycle complete[^"]*)" % ', feeder_src)
    assert printed, "the feeder prints no cycle-end line"
    line = printed.group(1).replace("%d", "3")
    status_src = STATUS.read_text(encoding="utf-8")
    pattern = re.search(r'CYCLE_END = re\.compile\(r"([^"]+)"\)', status_src).group(1)
    assert re.search(pattern, line), (pattern, line)
    # ...and it is not mistaken for a gauge line.
    gauge = re.search(r'GAUGE_LINE = re\.compile\(r"([^"]+)"\)', status_src).group(1)
    assert not re.search(gauge, line)


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 3 - the migration reads the REAL state of both timers, so a rerun
# can never put the old timer beside an active new one
# ═══════════════════════════════════════════════════════════════════════════

def test_gemini_enable_failure_never_leaves_the_migration_half_recorded_sr067(tmp_path):
    box = _legacy_box(tmp_path, "claude,gemini")
    first = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="enable --now homehub-gemini-refresh.timer")
    assert first.returncode == 1, first.stdout + first.stderr          # reported, not aborted
    assert _refreshers(box) == [NEW_TIMER]
    assert not (box.units / OLD_TIMER).exists(), "the migration was committed before the failing step"
    second = box.run(SETUP_USAGE)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _refreshers(box) == [NEW_TIMER]


def test_solrepro_new_timer_already_running_and_old_disable_failing_is_cleanup_not_restore_sr067(tmp_path):
    """Sol's two-run repro: the first run activated @claude and died before removing
    the old files. On the rerun the old timer must NEVER be re-enabled."""
    box = _legacy_box(tmp_path)
    (box.root / "enabled").write_text(NEW_TIMER + "\n", encoding="utf-8")        # old already off
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="disable --now " + OLD_TIMER)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _refreshers(box) == [NEW_TIMER]
    assert "systemctl enable --now " + OLD_TIMER not in box.calls
    assert not (box.units / OLD_TIMER).exists()
    assert "Migrated" in done.stdout


def test_both_timers_on_and_the_old_one_will_not_turn_off_is_a_loud_error_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    (box.root / "enabled").write_text(OLD_TIMER + "\n" + NEW_TIMER + "\n", encoding="utf-8")
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="disable --now " + OLD_TIMER)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "ERROR" in done.stdout and "systemctl disable --now " + OLD_TIMER in done.stdout
    assert "systemctl enable --now " + OLD_TIMER not in box.calls


def test_unreadable_timer_state_changes_nothing_and_says_how_to_check_sr067(tmp_path):
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="is-enabled")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "ERROR: cannot read the state of " + NEW_TIMER in done.stdout
    assert "systemctl is-enabled" in done.stdout
    assert _refreshers(box) == [OLD_TIMER]
    touched = [c for c in box.calls if c.startswith(("systemctl disable", "systemctl enable"))
               and c.split()[-1] in (OLD_TIMER, NEW_TIMER)]
    assert not touched, touched
    assert (box.units / OLD_TIMER).exists()


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 3 - the in-progress exception follows the CURRENT invocation, and a
# completed run whose POST failed is not "verified"
# ═══════════════════════════════════════════════════════════════════════════

def _claude_live_box(tmp_path):
    box, _ = _claude_box(tmp_path)
    box.cred("alice", ".claude/.credentials.json")
    required = [g.key for g in feeder.GAUGE_SPECS if g.source == "claude" and not g.optional]
    return box, [(k, "live") for k in required]


def test_a_good_b_failed_c_running_is_not_verified_llr1117(tmp_path):
    """Sol: A succeeded; B died without a cycle-end line; C is running but has not
    written anything yet. The newest journal invocation is B, not the running C."""
    box, live = _claude_live_box(tmp_path)
    box.journal({"gauges": live, "age": 900, "id": "A"},
                {"gauges": [], "complete": False, "extra": ["Traceback (most recent call last):"],
                 "age": 300, "id": "B"})
    out = _status(box, AI_CLI_USAGE_SERVICE_STATE="activating", AI_CLI_USAGE_INVOCATION_ID="C")
    assert "login claude: credential present (not verified)" in out
    # Had C been the newest journal invocation (B absent), borrowing A would be right.
    box.journal({"gauges": live, "age": 900, "id": "A"}, {"gauges": live[:1], "complete": False, "age": 5, "id": "C"})
    out = _status(box, AI_CLI_USAGE_SERVICE_STATE="activating", AI_CLI_USAGE_INVOCATION_ID="C")
    assert "login claude: verified" in out


def test_a_run_that_died_before_printing_anything_counts_as_the_newest_failed_run_llr1117(tmp_path):
    box, live = _claude_live_box(tmp_path)
    box.journal({"gauges": live, "age": 900, "id": "A"}, {"manager_only": True, "age": 60, "id": "B"})
    out = _status(box)
    assert "login claude: credential present (not verified)" in out


def test_live_but_post_failed_is_credentials_ok_not_verified_llr1117(tmp_path):
    box, live = _claude_live_box(tmp_path)
    failed = [(k, st, "POST FAILED") for k, st in live]
    box.journal({"gauges": failed})
    out = _status(box)
    assert "login claude: credentials OK, posting failing" in out
    assert "login claude: verified" not in out and "consumer operation verified" not in out
    assert "LOGINS OWED: none" in out
    # One required gauge posted, one not: still not verified.
    box.journal({"gauges": [live[0] + ("posted",), failed[1]]})
    assert "login claude: credentials OK, posting failing" in _status(box)
    box.journal({"gauges": live})
    assert "login claude: verified" in _status(box)


def test_half_applied_new_timer_that_cannot_be_turned_off_is_never_joined_by_the_old_one_sr067(tmp_path):
    """The new timer ends up enabled-but-not-started and `disable` fails too: the
    state cannot be established, so NOTHING more is changed (the old timer stays
    off) and the ERROR names the commands. Restoring blindly would leave both
    enabled at the next boot."""
    box = _legacy_box(tmp_path)
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="--now " + NEW_TIMER, STUB_ENABLE_NOSTART="1")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "ERROR: cannot tell whether %s is running" % NEW_TIMER in done.stdout
    assert "systemctl enable --now %s" % OLD_TIMER in done.stdout        # the instruction, not an action
    assert "systemctl enable --now " + OLD_TIMER not in box.calls         # ...never executed
    assert OLD_TIMER not in _enabled(box)


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 4 - an UNKNOWN timer state never leads to a disable (or an enable)
# ═══════════════════════════════════════════════════════════════════════════

def _timer_mutations(box):
    return [c for c in box.calls if c.startswith(("systemctl disable", "systemctl enable"))
            and c.split()[-1] in (OLD_TIMER, NEW_TIMER)]


def test_solrepro_migrated_hub_query_failing_after_the_enable_keeps_the_new_timer_sr067(tmp_path):
    """No legacy files, @claude already on; on the rerun `enable --now` succeeds and
    then the state queries fail. Round 3 disabled the replacement and left ZERO."""
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"],
              accounts=["alice"])
    (box.root / "enabled").write_text(NEW_TIMER + "\n", encoding="utf-8")
    done = box.run(SETUP_USAGE, STUB_QFAIL_AFTER="enable --now " + NEW_TIMER)
    assert done.returncode == 3, done.stdout + done.stderr
    assert NEW_TIMER in _enabled(box), "the replacement must NOT have been disabled"
    assert not [c for c in _timer_mutations(box) if c.startswith("systemctl disable")]
    assert "ERROR: cannot confirm the state of " + NEW_TIMER in done.stdout
    assert "systemctl is-active" in done.stdout


QUERY_FAILURES = [
    # id, legacy present?, extra env, expected exit, surviving refresher (file-based)
    ("unknown-after-enable-with-legacy", True, {"STUB_QFAIL_AFTER": "enable --now " + NEW_TIMER}, 3, NEW_TIMER),
    ("unknown-after-enable-no-legacy", False, {"STUB_QFAIL_AFTER": "enable --now " + NEW_TIMER}, 3, NEW_TIMER),
    ("unknown-at-start-with-legacy", True, {"STUB_SYSTEMCTL_FAIL": "is-enabled"}, 3, OLD_TIMER),
    ("unknown-at-restore-after-old-disabled", True,
     {"STUB_ACTIVE": "activating", "STUB_QFAIL_AFTER": "disable --now " + OLD_TIMER}, 3, OLD_TIMER),
]


@pytest.mark.parametrize("name,legacy,extra,code,survivor", QUERY_FAILURES, ids=[q[0] for q in QUERY_FAILURES])
def test_query_failures_never_disable_a_timer_and_never_leave_zero_sr067(tmp_path, name, legacy, extra, code, survivor):
    if legacy:
        box = _legacy_box(tmp_path)
    else:
        box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"],
                  accounts=["alice"])
        (box.root / "enabled").write_text("", encoding="utf-8")
    done = box.run(SETUP_USAGE, **extra)
    assert done.returncode == code, done.stdout + done.stderr
    assert _refreshers(box) == [survivor], (_enabled(box), done.stdout)
    # A timer that was enabled before the run is only ever disabled on a CONFIRMED
    # state: with the queries failing, the only disable allowed is the old timer's,
    # which happens before the failure begins.
    assert not [c for c in _timer_mutations(box) if c == "systemctl disable --now " + NEW_TIMER]


def test_the_no_legacy_enable_that_fails_outright_says_how_to_start_the_refresher_sr067(tmp_path):
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"], accounts=["alice"])
    done = box.run(SETUP_USAGE, STUB_SYSTEMCTL_FAIL="enable --now " + NEW_TIMER)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "ERROR: Claude account 1 has NO refresher" in done.stdout
    assert "systemctl enable --now " + NEW_TIMER in done.stdout
