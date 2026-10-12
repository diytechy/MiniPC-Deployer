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
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$SANDBOX/calls.log"
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
    "stat": r"""#!/usr/bin/env bash
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
            # Git Bash rewrites "/home" into a Windows path when it crosses into a
            # native process; the stub getent must see the literal value.
            "MSYS2_ENV_CONV_EXCL": "STUB_HOME_BASE",
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

    def cred(self, account, relative, content=CANARY):
        path = self.home / account / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


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
    assert "ReadWritePaths=%s/.claude %s/.claude.json" % (home, home) in claude
    claude2 = (box.units / "homehub-claude-refresh@claude2.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "CLAUDE_CONFIG_DIR=%s/.claude-2" % home in claude2
    gemini = (box.units / "homehub-gemini-refresh.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "ReadWritePaths=%s/.gemini" % home in gemini
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
    (box.root / "feeder.log").write_text(
        "ai-usage: ai-usage-claude-session        live         12\n"
        "ai-usage: ai-usage-claude2-session       UNAVAILABLE  x\n", encoding="utf-8")
    out = _status(box)
    assert "login claude: verified" in out and "consumer operation verified" in out
    # A later UNAVAILABLE for the same gauge cancels the earlier live.
    (box.root / "feeder.log").write_text(
        "ai-usage: ai-usage-claude-session        live         12\n"
        "ai-usage: ai-usage-claude-session        UNAVAILABLE  x\n", encoding="utf-8")
    assert "login claude: credential present (not verified)" in _status(box)


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
    return text, start, text[start:end]


def test_firstboot_hook_only_schedules_and_runs_before_6d_llr1119():
    text, start, hook = _hook_text()
    assert "--schedule" in hook
    assert "--run" not in hook, "firstboot must never wait on a download"
    assert hook.index("ensure-ai-account.sh\" \"$acct\"") < hook.index("provision-ai-clis.sh\" --schedule")
    call = text.index("\nai_cli_provision_hook\n")
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
    assert (box.units / "homehub-ai-clis-provision.timer").exists()
    assert (box.units / "homehub-ai-clis-provision.service").exists()
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

def test_real_provisioner_contract(tmp_path):
    provisioner = CLI_DIR / "provision-ai-clis.sh"
    if not provisioner.exists():
        pytest.skip("NOT RUN: provision-ai-clis.sh is not in this tree yet (W1 has not merged)")
    box = Box(tmp_path, USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    done = subprocess.run([box.bash, provisioner.as_posix(), "--status", "--json"],
                          env=box.env(AI_CLI_PREFIX=prefix.as_posix()), capture_output=True, text=True)
    shape = json.loads(done.stdout)
    assert shape["provisioning"] in ("complete", "incomplete", "running", "off")
    assert set(shape["clis"]) >= {"claude", "codex", "opencode", "gemini", "node"}
    out = _status(box, AI_CLI_PROVISIONER=provisioner.as_posix(), AI_CLI_PREFIX=prefix.as_posix())
    assert out.startswith("PROVISIONING:"), out
