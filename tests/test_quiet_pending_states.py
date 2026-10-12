"""Quiet, expected states are not errors (Owner 2026-10-11: "prevents the user from
having to deal with nuisance error codes about missing packages ... That might still
require the user to login").

"NOT INSTALLED YET (provisioning)" and "LOGIN OWED" post UNAVAILABLE, log ONE INFO
line and do NOT fail a unit. A genuine error still fails. One truthiness rule runs
through every component.

Verifies: SR-079, SR-080 (LLR-1113..LLR-1120) - seams-review fixes.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_cli_integration as ti  # noqa: E402  (Box, scripts, stubs: one sandbox, not a second one)

feeder = ti.feeder
REPO = ti.REPO


# ═══════════════════════════════════════════════════════════════════════════
# The feeder: pending install / login owed are notes, not failures
# ═══════════════════════════════════════════════════════════════════════════

class Rec(object):
    def __init__(self, ok=True):
        self.bodies, self.ok = [], ok

    def __call__(self, body, url, env, timeout):
        self.bodies.append(body)
        return self.ok, "HTTP 200" if self.ok else "HTTP 500"


def _cycle_env(tmp_path, sources, **extra):
    env = {"AI_USAGE_ENABLED": "true", "AI_USAGE_USER": "108000000000000000001",
           "AI_USAGE_STATE_FILE": str(tmp_path / "usage-state.json"),
           "AI_USAGE_SOURCES": sources, "STATE_DIRECTORY": str(tmp_path),
           "_identity": "108000000000000000001", "_feed_url": "http://127.0.0.1:8787/api/feed"}
    env.update(extra)
    return env


def _run(tmp_path, sources, **extra):
    env = _cycle_env(tmp_path, sources, **extra)
    rec = Rec()
    posted, failures = feeder.run_cycle(env, now=1788944000, poster=rec)
    return env, rec, posted, failures


def test_codex_binary_absent_is_pending_install_not_a_failure(tmp_path):
    env, rec, posted, failures = _run(tmp_path, "codex", AI_USAGE_CODEX_BIN=str(tmp_path / "no-such-codex"))
    assert failures == []
    assert len(env["_notes"]) == 1 and "codex: not installed yet" in env["_notes"][0]
    assert posted and all(fresh is False for _, fresh, _ok in posted)       # UNAVAILABLE as before
    assert "Traceback" not in "".join(env["_notes"])


def test_the_provisioners_reason_is_mentioned_when_one_is_recorded(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps(
        {"clis": {"codex": {"pending": {"reason": "download in progress"}}}}), encoding="utf-8")
    env, _, _, failures = _run(tmp_path, "codex", AI_USAGE_CODEX_BIN=str(tmp_path / "nope"),
                               AI_CLIS_STATE_DIR=str(state))
    assert failures == []
    assert "(provisioning: download in progress)" in env["_notes"][0]


@pytest.mark.parametrize("source,knob", [("claude", "AI_USAGE_CLAUDE_CREDENTIALS"),
                                         ("claude2", "AI_USAGE_CLAUDE2_CREDENTIALS"),
                                         ("gemini", "AI_USAGE_GEMINI_CREDENTIALS"),
                                         ("opencode", "AI_USAGE_OPENCODE_AUTH")])
@pytest.mark.parametrize("how", ["absent-file", "unset-knob"])
def test_missing_credential_is_login_owed_not_a_failure(tmp_path, source, knob, how):
    extra = {knob: str(tmp_path / "nope" / "cred.json")} if how == "absent-file" else {}
    env, rec, posted, failures = _run(tmp_path, source, **extra)
    assert failures == [], failures
    assert len(env["_notes"]) == 1 and "login owed" in env["_notes"][0], env.get("_notes")
    assert posted and all(fresh is False for _, fresh, _ok in posted)


def test_codex_without_a_signin_under_its_home_is_login_owed(tmp_path):
    env, _, _, failures = _run(tmp_path, "codex", CODEX_HOME=str(tmp_path / "codex-home"),
                               AI_USAGE_CODEX_BIN=str(tmp_path / "never-launched"))
    assert failures == [] and "login owed" in env["_notes"][0]


def _main(tmp_path, monkeypatch, capsys, sources, post_ok=True, **extra):
    env = _cycle_env(tmp_path, sources, **extra)
    env["AI_USAGE_FEED_URL"] = env["_feed_url"]
    for key in list(os.environ):
        if key.startswith(("AI_USAGE_", "AI_CLIS_")):
            monkeypatch.delenv(key)
    for key, value in env.items():
        if not key.startswith("_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(feeder, "post_gauge", Rec(post_ok))
    rc = feeder.main([])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_unit_exits_0_with_one_info_line_per_quiet_source(tmp_path, monkeypatch, capsys):
    rc, out, err = _main(tmp_path, monkeypatch, capsys, "codex,claude",
                         AI_USAGE_CODEX_BIN=str(tmp_path / "nope"),
                         AI_USAGE_CLAUDE_CREDENTIALS=str(tmp_path / "none.json"))
    assert rc == 0, (out, err)
    info = [l for l in out.splitlines() if l.startswith("ai-usage: INFO ")]
    assert len(info) == 2 and "not installed yet" in info[0] and "login owed" in info[1]
    assert err == ""


def test_a_genuine_failure_still_exits_1(tmp_path, monkeypatch, capsys):
    # A credential that EXISTS but is not JSON is an error, not "login owed".
    bad = tmp_path / "cred.json"
    bad.write_text("not json", encoding="utf-8")
    rc, out, err = _main(tmp_path, monkeypatch, capsys, "claude", AI_USAGE_CLAUDE_CREDENTIALS=str(bad))
    assert rc == 1 and "unreadable" in err
    # A post that the tracker refuses is an error too, quiet sources notwithstanding.
    rc, out, err = _main(tmp_path, monkeypatch, capsys, "codex", post_ok=False,
                         AI_USAGE_CODEX_BIN=str(tmp_path / "nope"))
    assert rc == 1 and "post " in err


# ═══════════════════════════════════════════════════════════════════════════
# The keepers: skipped quietly until there is something to keep alive
# ═══════════════════════════════════════════════════════════════════════════
#
# Chosen: ExecCondition= gates (NOT Condition*=|, which systemd merges into one OR group), not "enable the timer only once the file exists"
# and not SuccessExitStatus=. A failed condition SKIPS the start (no `failed` state,
# no exit code, nothing to special-case), the timer stays enabled so the first run
# after the sign-in needs no re-run of setup, and it needs no shell exit-code
# convention shared between three programs. SuccessExitStatus= would also hide a
# real exit 3 ("REFUSED") from the Gemini keeper's other causes.

def test_keeper_timers_stay_enabled_so_the_first_run_after_signin_needs_no_rerun(tmp_path):
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude,claude2,gemini",
                                          "AI_USAGE_USER_ACCOUNT=alice"])
    assert box.run(ti.SETUP_USAGE).returncode == 0
    enabled = (box.root / "enabled").read_text(encoding="utf-8").split()
    assert {"homehub-claude-refresh@claude2.timer", "homehub-gemini-refresh.timer"} <= set(enabled)


# ═══════════════════════════════════════════════════════════════════════════
# One truthiness rule everywhere
# ═══════════════════════════════════════════════════════════════════════════

ENABLED_FORMS = ["true", '"true"', "'true'"]
DISABLED_FORMS = ["True", "TRUE", '"True"', "yes", "1", "false", ""]


def _classify_all(tmp_path, value):
    """-> {component: bool} for AI_CLI_ENABLED=<value> (usage off, user alice)."""
    box = ti.Box(tmp_path, ["AI_CLI_ENABLED=%s" % value, "AI_CLI_USER=alice", "AI_USAGE_ENABLED=false",
                            "AI_USAGE_USER_ACCOUNT=bob"], accounts=["alice", "bob"])
    out = {}
    # ensure-ai-account.sh --effective
    # (the value travels in the environment: a Windows command line eats quotes)
    eff = subprocess.run([box.bash, "-c", 'bash "$1" --effective "$V" alice bob', "_", ti.ENSURE.as_posix()],
                         env=box.env(V=value), capture_output=True, text=True).stdout.strip()
    out["ensure --effective"] = eff == "alice"
    # the provisioner: does an enabled ai-cli make claude "needed"?
    seams = ti._real_seams(box, tmp_path)
    shape = json.loads(subprocess.run(
        [box.bash, (ti.CLI_DIR / "provision-ai-clis.sh").as_posix(), "--status", "--json"],
        env=box.env(**seams), capture_output=True, text=True).stdout)
    out["provision-ai-clis.sh"] = shape["provisioning"] == "incomplete"
    # the status step: does it check the ai-cli route's login?
    st = subprocess.run([box.bash, ti.STATUS.as_posix()],
                        env=box.env(AI_CLI_PROVISIONER=(ti.CLI_DIR / "provision-ai-clis.sh").as_posix(), **seams),
                        capture_output=True, text=True).stdout
    out["ai-cli-status.sh"] = "login claude (ai-cli)" in st
    # setup-ai-cli.sh: does it go on to install?
    setup = box.run(ti.SETUP_CLI)
    out["setup-ai-cli.sh"] = "nothing installed" not in setup.stdout
    return out


@pytest.mark.parametrize("value", ENABLED_FORMS, ids=["plain", "dquoted", "squoted"])
def test_exact_true_and_quoted_true_enable_every_component(tmp_path, value):
    seen = _classify_all(tmp_path, value)
    assert all(seen.values()), seen


@pytest.mark.parametrize("value", DISABLED_FORMS,
                         ids=["Title", "UPPER", "dquoted-Title", "yes", "one", "false", "empty"])
def test_anything_else_disables_every_component_identically(tmp_path, value):
    seen = _classify_all(tmp_path, value)
    assert not any(seen.values()), seen


# ═══════════════════════════════════════════════════════════════════════════
# Status wording, one status per firstboot, refuse before scheduling downloads
# ═══════════════════════════════════════════════════════════════════════════

def test_provisioning_off_says_not_installed_not_being_provisioned(tmp_path):
    status = json.loads(json.dumps(ti.ALL_INSTALLED))
    status["provisioning"] = "off"
    status["clis"]["claude"].update(installed=False, version=None)
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"], status=status)
    out = ti._status(box)
    assert "claude: not installed - provisioning is off" in out
    assert "being provisioned" not in out


def test_setup_scripts_stay_quiet_about_status_when_firstboot_asks(tmp_path):
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    loud = box.run(ti.SETUP_USAGE)
    quiet = box.run(ti.SETUP_USAGE, AI_CLI_SKIP_STATUS="1")
    assert "PROVISIONING:" in loud.stdout and "LOGINS OWED" in loud.stdout
    assert "PROVISIONING:" not in quiet.stdout and "LOGINS OWED" not in quiet.stdout
    text = ti.FIRSTBOOT.read_text(encoding="utf-8")
    assert text.count("AI_CLI_SKIP_STATUS=1 bash") == 2             # both setup calls
    assert text.count("ai-cli-status.sh") >= 1
    tail = text[text.index("# What provisioning and the sign-ins look like right now"):]
    assert "ai-cli-status.sh" in tail[:400], "the one summary stays at the end"


def _hook_with_usage(box, tmp_path, env_lines):
    import shutil
    stack = tmp_path / "stack-dir"
    shutil.copytree(str(ti.CLI_DIR), str(stack / "ai-cli"), ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.copy(str(box.root / "provision-ai-clis.sh"), str(stack / "ai-cli" / "provision-ai-clis.sh"))
    shutil.copytree(str(ti.USAGE_DIR), str(stack / "ai-usage"), ignore=shutil.ignore_patterns("__pycache__"))
    (stack / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    _, _, hook = ti._hook_text()
    script = tmp_path / "hook3.sh"
    script.write_text(
        "set -euo pipefail\nSTACK_DIR=%s\nlog() { echo \"[firstboot] $*\"; }\n"
        "_env_val() { sed -n \"s/^$1=//p\" \"$STACK_DIR/.env\" 2>/dev/null | head -1 | tr -d '\\r'; }\n"
        "AI_EFFECTIVE_ACCOUNT=\"\"\n%s\nai_cli_provision_hook\n" % (stack.as_posix(), hook), encoding="utf-8")
    return subprocess.run([box.bash, script.as_posix()],
                          env=box.env(AI_USAGE_ENV_FILE=(stack / ".env").as_posix()),
                          capture_output=True, text=True)


def test_a_blank_usage_identity_schedules_no_downloads(tmp_path):
    box = ti.Box(tmp_path, [])
    done = _hook_with_usage(box, tmp_path, ["AI_USAGE_ENABLED=true", "AI_USAGE_USER=",
                                            "AI_USAGE_FEED_URL=http://127.0.0.1:8787/api/feed",
                                            "AI_USAGE_USER_ACCOUNT=alice"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "settings are incomplete" in done.stdout
    assert "provisioner --schedule" not in box.calls and box.accounts == []
    # A complete configuration still schedules.
    (tmp_path / "ok").mkdir()
    box2 = ti.Box(tmp_path / "ok", [])
    done = _hook_with_usage(box2, tmp_path / "ok", ti.USAGE_ENV + ["AI_USAGE_USER_ACCOUNT=alice"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "provisioner --schedule" in box2.calls


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1: quiet means GENUINELY ABSENT - present-but-unusable stays loud
# ═══════════════════════════════════════════════════════════════════════════

import configparser  # noqa: E402
import shutil  # noqa: E402

import test_ai_usage_sources_and_auth as ts  # noqa: E402  (CLAUDE_BODY, NOW)


def _ini(text):
    parser = configparser.ConfigParser(strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    parser.read_string(text)
    return parser


def _exec_conditions(text):
    """The ExecCondition= values in a unit/drop-in text (a list setting: repeated keys)."""
    return [l.split("=", 1)[1] for l in text.splitlines() if l.startswith("ExecCondition=")]


def _sections(text):
    """{section: [raw 'Key=value' lines]} - keeps repeated keys, which configparser cannot."""
    out, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return out


KEEPERS = [("homehub-claude-refresh@.service", "claude"), ("homehub-gemini-refresh.service", "gemini")]
DROPINS = [("homehub-claude-refresh@claude.service.d", ".claude/.credentials.json"),
           ("homehub-claude-refresh@claude2.service.d", ".claude-2/.credentials.json"),
           ("homehub-gemini-refresh.service.d", ".gemini/oauth_creds.json")]


@pytest.mark.parametrize("unit,binary", KEEPERS, ids=["claude", "gemini"])
def test_keeper_cli_gate_is_one_execcondition_not_a_condition_or_group(unit, binary):
    secs = _sections((ti.USAGE_DIR / unit).read_text(encoding="utf-8"))
    # systemd merges every `Condition*=|` of a unit AND its drop-ins into ONE OR group
    # ("CLI present OR credential present"), so no Condition*= line may carry a gate.
    assert not [l for sec in secs.values() for l in sec if l.startswith("Condition")], secs
    gates = [l for l in secs["Service"] if l.startswith("ExecCondition=")]
    assert len(gates) == 1 and gates[0].endswith(" gate /usr/local/bin/" + binary), gates
    assert not [l for l in secs.get("Unit", []) if l.startswith("ExecCondition")]


def test_credential_gates_are_execconditions_in_service_added_by_each_drop_in(tmp_path):
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude,claude2,gemini",
                                          "AI_USAGE_USER_ACCOUNT=alice"])
    assert box.run(ti.SETUP_USAGE, STUB_HOME_BASE="/home").returncode == 0
    mapping = {v[1] for v in ti.MAPPINGS.values()}
    base = {u: _exec_conditions((ti.USAGE_DIR / u).read_text(encoding="utf-8")) for u, _ in KEEPERS}
    for dropin, rel in DROPINS:
        assert rel in mapping
        secs = _sections((box.units / dropin / "10-account.conf").read_text(encoding="utf-8"))
        assert not [l for sec in secs.values() for l in sec if l.startswith("Condition")], dropin
        gates = [l for l in secs["Service"] if l.startswith("ExecCondition=")]
        assert len(gates) == 1 and gates[0].endswith(" gate /home/alice/" + rel), (dropin, gates)
        assert not [l for l in secs.get("Unit", []) if l.startswith("ExecCondition")]
        # it ADDS to the base unit's list (never an empty reset assignment that would drop it)
        assert not [l for l in secs["Service"] if l == "ExecCondition="], dropin
    assert all(len(v) == 1 for v in base.values())


def _shq(text):
    return "'" + text.replace("'", "'\\''") + "'"


GATE_MATRIX = r"""set -u
W=$(mktemp -d); U=/run/systemd/system
cleanup() { for i in $(seq 1 12); do systemctl stop w7gate-$i.service >/dev/null 2>&1; rm -rf $U/w7gate-$i.service $U/w7gate-$i.service.d; done; systemctl daemon-reload; rm -rf "$W"; }
trap cleanup EXIT
BASE=@@BASE@@
DROP=@@DROP@@
mkdir -p $W/bin
touch $W/cred-present; ln -s $W/nowhere $W/cred-dangling
printf '#!/bin/sh\n' > $W/bin/exec; chmod 755 $W/bin/exec
printf 'not executable\n' > $W/bin/plain; chmod 644 $W/bin/plain
ln -s $W/nowhere $W/bin/dangling
i=0
for cli in absent exec plain dangling; do
  for cred in absent present dangling; do
    i=$((i+1))
    c="$W/bin/$cli"; k="$W/cred-$cred"
    [ "$cred" = absent ] && k="$W/cred-absent"
    mkdir -p $U/w7gate-$i.service.d
    printf '[Unit]\nDescription=w7 gate test\n[Service]\nType=oneshot\nExecCondition=%s\nExecStart=/bin/touch %s\n' "${BASE//@@CLI@@/$c}" "$W/ran-$i" > $U/w7gate-$i.service
    printf '[Service]\nExecCondition=%s\n' "${DROP//@@CRED@@/$k}" > $U/w7gate-$i.service.d/10-cred.conf
    CASES[$i]="$cli $cred"
  done
done
systemctl daemon-reload
for n in $(seq 1 $i); do
  systemctl start w7gate-$n.service >/dev/null 2>&1
  ran=0; [ -e $W/ran-$n ] && ran=1
  failed=$(systemctl is-failed w7gate-$n.service 2>/dev/null)
  echo "CASE ${CASES[$n]} ran=$ran state=$failed"
done
"""


def test_real_systemd_runs_a_keeper_only_when_BOTH_the_cli_and_the_login_are_there(tmp_path):
    """The merged base unit + drop-in, on the real PID 1 systemd (WSL): ExecCondition
    lines accumulate and must ALL pass. Absent = nothing at the path (no file, no
    link); a dangling link or a non-executable file is 'there' and starts."""
    unit = (ti.USAGE_DIR / "homehub-claude-refresh@.service").read_text(encoding="utf-8")
    base = _exec_conditions(unit)[0].replace("/usr/local/bin/claude", "@@CLI@@")
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    assert box.run(ti.SETUP_USAGE, STUB_HOME_BASE="/home").returncode == 0
    drop = _exec_conditions((box.units / "homehub-claude-refresh@claude.service.d" / "10-account.conf")
                            .read_text(encoding="utf-8"))[0].replace("/home/alice/.claude/.credentials.json", "@@CRED@@")
    script = GATE_MATRIX.replace("@@BASE@@", _shq(base)).replace("@@DROP@@", _shq(drop))
    done = ti._wsl_root(script, tmp_path)
    if "Failed to connect to bus" in done.stderr or "System has not been booted" in done.stderr:
        pytest.skip("WSL has no running systemd")
    rows = re.findall(r"CASE (\w+) (\w+) ran=(\d) state=(\w+)", done.stdout)
    assert len(rows) == 12, (done.stdout, done.stderr)
    for cli, cred, ran, state in rows:
        expect = "1" if (cli != "absent" and cred != "absent") else "0"
        assert ran == expect, (cli, cred, ran)
        # a gate that skips is NOT a failure of the unit
        assert state != "failed", (cli, cred, state)


VERIFY = r"""set -u
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
cp -r @@DIR@@/sandbox/units/. $W/
chmod -R a-x $W; find $W -type d -exec chmod 755 {} +
for u in homehub-claude-refresh@claude.service homehub-claude-refresh@claude2.service homehub-gemini-refresh.service; do
  SYSTEMD_UNIT_PATH=$W: systemd-analyze verify --man=no $W/$u 2>&1 | sed "s/^/VERIFY $u: /"
done
# positive control: an ExecCondition under [Unit] IS flagged by systemd itself
mkdir -p $W/homehub-gemini-refresh.service.d
printf '[Unit]\nExecCondition=/bin/true\n' > $W/homehub-gemini-refresh.service.d/99-bad.conf
SYSTEMD_UNIT_PATH=$W: systemd-analyze verify --man=no $W/homehub-gemini-refresh.service 2>&1 | sed 's/^/CONTROL: /'
"""


def test_systemd_itself_accepts_the_generated_drop_ins_and_flags_a_misplaced_gate(tmp_path):
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude,claude2,gemini",
                                          "AI_USAGE_USER_ACCOUNT=alice"])
    assert box.run(ti.SETUP_USAGE, STUB_HOME_BASE="/home").returncode == 0
    done = ti._wsl_root(VERIFY, tmp_path)
    if "systemd-analyze" in done.stderr and "not found" in done.stderr:
        pytest.skip("no systemd-analyze in WSL")
    out = done.stdout
    verify_lines = [l for l in out.splitlines() if l.startswith("VERIFY ")]
    assert not [l for l in verify_lines if "Unknown key" in l or "ExecCondition" in l or "Condition" in l], verify_lines
    control = [l for l in out.splitlines() if l.startswith("CONTROL:")]
    assert any("Unknown key name 'ExecCondition' in section 'Unit'" in l for l in control), (control, done.stderr)


# ---- the feeder: only ENOENT/ENOTDIR is "absent" -------------------------------------------

def test_path_absent_is_true_only_for_enoent_and_enotdir(monkeypatch):
    import errno
    for exc, expect in ((FileNotFoundError(errno.ENOENT, "x"), True), (NotADirectoryError(errno.ENOTDIR, "x"), True),
                        (PermissionError(errno.EACCES, "x"), False), (OSError(errno.EIO, "x"), False)):
        def boom(path, _exc=exc):
            raise _exc
        monkeypatch.setattr(feeder.os, "lstat", boom)
        assert feeder.path_absent("/anything") is expect, exc
    monkeypatch.undo()
    assert feeder.path_absent(__file__) is False             # present


def test_a_credential_behind_a_directory_we_may_not_enter_is_a_failure_not_login_owed(tmp_path, monkeypatch):
    """The 0700 home of another user: opening the credential raises PermissionError."""
    cred = tmp_path / "home" / ".claude" / ".credentials.json"
    real_open = open

    def deny(path, *a, **k):
        if str(path) == str(cred):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", deny)
    env, _, _, failures = _run(tmp_path, "claude", AI_USAGE_CLAUDE_CREDENTIALS=str(cred))
    assert any("unreadable: PermissionError" in f for f in failures), failures
    assert "_notes" not in env or not env["_notes"]


def test_a_denied_lstat_on_the_codex_home_does_not_pretend_the_login_is_owed(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    real_lstat = os.lstat

    def lstat(path, *a, **k):
        if str(path).startswith(str(home)):
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(feeder.os, "lstat", lstat)
    env, _, _, failures = _run(tmp_path, "codex", CODEX_HOME=str(home), AI_USAGE_CODEX_BIN=str(tmp_path / "no-such"))
    # not "login owed": it went on to launch codex, which is itself absent here -> pending install
    assert not any("login owed" in n for n in env.get("_notes", []))


def test_launch_enoent_with_the_binary_present_is_a_failure(tmp_path, monkeypatch):
    present = tmp_path / "codex"
    present.write_text("#!/nonexistent-interpreter\n", encoding="utf-8")
    present.chmod(0o755)

    def popen(*a, **k):
        raise FileNotFoundError(2, "No such file or directory")       # e.g. a missing interpreter

    monkeypatch.setattr(feeder.subprocess, "Popen", popen)
    env, _, _, failures = _run(tmp_path, "codex", AI_USAGE_CODEX_BIN=str(present))
    assert any("codex: cannot launch" in f for f in failures), failures
    assert not env.get("_notes")
    # ...and with nothing at the path the same launch error IS "not installed yet".
    env2, _, _, failures2 = _run(tmp_path, "codex", AI_USAGE_CODEX_BIN=str(tmp_path / "gone"))
    assert failures2 == [] and "not installed yet" in env2["_notes"][0]


def test_a_dangling_link_is_present_so_its_launch_failure_stays_loud(tmp_path):
    link = tmp_path / "codex"
    try:
        os.symlink(str(tmp_path / "nowhere"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert feeder.binary_absent(str(link), {}) is False
    assert feeder.binary_absent(str(tmp_path / "gone"), {}) is True
    assert feeder.binary_absent("codex-nowhere-xyz", {"PATH": str(tmp_path)}) is True


def test_pending_source_posts_unavailable_never_the_cached_reading(tmp_path, monkeypatch):
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-xyz"}}), encoding="utf-8")
    monkeypatch.setattr(feeder, "http_json", lambda *a, **k: ts.CLAUDE_BODY)
    env = _cycle_env(tmp_path, "claude", AI_USAGE_CLAUDE_CREDENTIALS=str(cred))
    rec1 = Rec()
    feeder.run_cycle(env, now=ts.NOW, poster=rec1)
    assert any("observed_at" in b for b in rec1.bodies), "the first cycle posted live readings"
    # Ten minutes later the login is gone. The cached reading is still within every horizon.
    cred.unlink()
    env2 = _cycle_env(tmp_path, "claude", AI_USAGE_CLAUDE_CREDENTIALS=str(cred))
    rec2 = Rec()
    posted, failures = feeder.run_cycle(env2, now=ts.NOW + 600, poster=rec2)
    assert failures == [] and "login owed" in env2["_notes"][0]
    claude_bodies = [b for b in rec2.bodies if "claude" in b["id"]]
    assert claude_bodies and all("observed_at" not in b for b in claude_bodies), claude_bodies
    assert all(fresh is False for _, fresh, _ok in posted)


# ---- ONE unquote ------------------------------------------------------------------------------

def test_single_quoted_true_inside_double_quotes_is_off_everywhere_and_picks_one_account(tmp_path):
    """AI_CLI_ENABLED="'true'": one pair is stripped, leaving 'true' - not true. Every
    component must agree it is DISABLED and use the same (usage) account."""
    box = ti.Box(tmp_path, ["AI_CLI_ENABLED=\"'true'\"", "AI_CLI_USER=alice", "AI_USAGE_ENABLED=true",
                            "AI_USAGE_USER=108000000000000000001",
                            "AI_USAGE_FEED_URL=http://127.0.0.1:8787/api/feed",
                            "AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=bob"])
    box.env_file.write_text(box.env_file.read_text(encoding="utf-8") + "AI_USAGE_CLAUDE_CREDENTIALS=%s\n"
                            % (box.home / "bob" / ".claude" / ".credentials.json").as_posix(), encoding="utf-8")
    eff = subprocess.run([box.bash, "-c", 'bash "$1" --effective "$V" alice bob', "_", ti.ENSURE.as_posix()],
                         env=box.env(V="\"'true'\""), capture_output=True, text=True).stdout.strip()
    assert eff == "bob"
    done = box.run(ti.SETUP_USAGE)
    assert done.returncode == 0, done.stdout + done.stderr
    dropin = (box.units / "homehub-ai-usage.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "User=bob" in dropin and "alice" not in dropin
    assert box.accounts == ["bob"]
    status = ti._status(box)
    assert "sudo -u bob" in status and "alice" not in status
    # firstboot's hook agrees: it creates BOB, never alice.
    (tmp_path / "fb").mkdir()
    box2 = ti.Box(tmp_path / "fb", [])
    done = _hook_with_usage(box2, tmp_path / "fb", [
        "AI_CLI_ENABLED=\"'true'\"", "AI_CLI_USER=alice", "AI_USAGE_ENABLED=true",
        "AI_USAGE_USER=108000000000000000001", "AI_USAGE_FEED_URL=http://127.0.0.1:8787/api/feed",
        "AI_USAGE_USER_ACCOUNT=bob"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert box2.accounts == ["bob"], box2.accounts


def test_a_dangling_credential_link_is_present_and_fails_loudly(tmp_path, monkeypatch):
    """open() says ENOENT for a dangling link, but lstat finds the link: not absent."""
    cred = tmp_path / "cred.json"
    real_open = open

    def enoent(path, *a, **k):
        if str(path) == str(cred):
            raise FileNotFoundError(2, "No such file or directory")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", enoent)
    real_lstat = os.lstat
    monkeypatch.setattr(feeder.os, "lstat", lambda p, *a, **k: real_lstat(__file__) if str(p) == str(cred) else real_lstat(p, *a, **k))
    env, _, _, failures = _run(tmp_path, "claude", AI_USAGE_CLAUDE_CREDENTIALS=str(cred))
    assert any("unreadable: FileNotFoundError" in f for f in failures), failures
    assert not env.get("_notes")


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 2
# ═══════════════════════════════════════════════════════════════════════════

# ---- every firstboot gate uses the ONE unquote rule ------------------------------------------

def _gate_value(tmp_path, key, raw):
    """Run firstboot.sh's OWN assignment line for `key` (6d / 6e), against an .env."""
    text = ti.FIRSTBOOT.read_text(encoding="utf-8")
    line = [l for l in text.splitlines() if re.match(r"^%s=\"" % key, l)]
    assert len(line) == 1, (key, line)
    u0 = text.index("ai_unq() {")
    unq = text[u0:text.index("\n}\n", u0) + 3]
    stack = tmp_path / "gate-stack"
    stack.mkdir(exist_ok=True)
    (stack / ".env").write_text("%s=%s\n" % (key, raw), encoding="utf-8")
    script = tmp_path / "gate.sh"
    script.write_text(
        "set -euo pipefail\nSTACK_DIR=%s\n"
        "_env_val() { sed -n \"s/^$1=//p\" \"$STACK_DIR/.env\" 2>/dev/null | head -1 | tr -d '\\r'; }\n%s\n%s\n"
        "[ \"${%s:-false}\" = \"true\" ] && echo ON || echo OFF\n" % (stack.as_posix(), unq, line[0], key),
        encoding="utf-8")
    box = ti.Box(tmp_path, [])
    out = subprocess.run([box.bash, script.as_posix()], env=box.env(), capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.strip()


@pytest.mark.parametrize("key", ["AI_CLI_ENABLED", "AI_USAGE_ENABLED"])
@pytest.mark.parametrize("raw,expect", [("true", "ON"), ('"true"', "ON"), ("'true'", "ON"),
                                        ("True", "OFF"), ("\"'true'\"", "OFF"), ("false", "OFF")],
                         ids=["plain", "dq", "sq", "Title", "dq-sq", "false"])
def test_6d_and_6e_gates_agree_with_the_hook_on_every_form(tmp_path, key, raw, expect):
    assert _gate_value(tmp_path, key, raw) == expect


def test_no_ai_gate_in_firstboot_still_strips_only_double_quotes():
    text = ti.FIRSTBOOT.read_text(encoding="utf-8")
    offenders = [l for l in text.splitlines()
                 if re.search(r"(AI_CLI|AI_USAGE)_[A-Z_]+\"?\)?.*tr -d '\"'", l) and "ai_unq" not in l]
    assert offenders == [], offenders


# ---- a dangling ANCESTOR is a failure; a really missing parent is "login owed" ---------------------

class FakeFs(object):
    """A tiny virtual filesystem behind os.lstat / os.stat: path -> 'dir' | 'file' | 'dangling'."""

    def __init__(self, nodes):
        self.nodes = nodes

    def _st(self, path, follow):
        kind = self.nodes.get(path.replace("\\", "/"))
        if kind is None:
            raise FileNotFoundError(2, "No such file or directory", path)
        if kind == "dangling":
            if follow:
                raise FileNotFoundError(2, "No such file or directory", path)
            return os.stat_result((0o120777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return os.stat_result((0o040755 if kind == "dir" else 0o100644, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def lstat(self, path, *a, **k):
        return self._st(str(path), False)

    def stat(self, path, *a, **k):
        return self._st(str(path), True)


def _fake(monkeypatch, nodes):
    fs = FakeFs(nodes)
    monkeypatch.setattr(feeder.os, "lstat", fs.lstat)
    monkeypatch.setattr(feeder.os, "stat", fs.stat)


def test_dangling_ancestor_symlink_is_not_absent(monkeypatch):
    _fake(monkeypatch, {"/home": "dir", "/home/alice": "dir", "/home/alice/.claude": "dangling"})
    assert feeder.path_absent("/home/alice/.claude/.credentials.json") is False
    # a dangling link two levels up is found too
    _fake(monkeypatch, {"/home": "dir", "/home/alice": "dangling"})
    assert feeder.path_absent("/home/alice/.claude/.credentials.json") is False


def test_really_missing_parent_directory_is_absent(monkeypatch):
    _fake(monkeypatch, {"/home": "dir", "/home/alice": "dir"})                 # no .claude at all
    assert feeder.path_absent("/home/alice/.claude/.credentials.json") is True
    assert feeder.path_absent("/home/alice/.claude/deeper/x.json") is True


def test_a_valid_symlinked_ancestor_with_nothing_inside_is_still_absent(monkeypatch):
    class Fs(FakeFs):
        def _st(self, path, follow):
            if path == "/home/alice/.claude":                # a link whose target exists
                return os.stat_result((0o120777 if not follow else 0o040755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return FakeFs._st(self, path, follow)
    fs = Fs({"/home": "dir", "/home/alice": "dir"})
    monkeypatch.setattr(feeder.os, "lstat", fs.lstat)
    monkeypatch.setattr(feeder.os, "stat", fs.stat)
    assert feeder.path_absent("/home/alice/.claude/.credentials.json") is True


def test_the_dangling_ancestor_makes_the_claude_reader_fail_loudly(tmp_path, monkeypatch):
    cred = "/home/alice/.claude/.credentials.json"
    _fake(monkeypatch, {"/home": "dir", "/home/alice": "dir", "/home/alice/.claude": "dangling"})
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: (_ for _ in ()).throw(FileNotFoundError(2, "x"))
                        if str(p) == cred else real_open(p, *a, **k))
    env, _, _, failures = _run(tmp_path, "claude", AI_USAGE_CLAUDE_CREDENTIALS=cred)
    assert any("unreadable: FileNotFoundError" in f for f in failures), failures
    assert not env.get("_notes")


ANCESTORS = r"""set -u
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
mkdir -p $W/home/a $W/home/b
ln -s $W/nowhere $W/home/a/.claude          # dangling ancestor
mkdir -p $W/home/b/.other                   # b has NO .claude at all
python3 - "$W" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("f", "@@REPO@@/stack/ai-usage/ai_usage_feeder.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
w = sys.argv[1]
print("DANGLING-PARENT absent=%s" % m.path_absent(w + "/home/a/.claude/.credentials.json"))
print("MISSING-PARENT absent=%s" % m.path_absent(w + "/home/b/.claude/.credentials.json"))
print("MISSING-FILE absent=%s" % m.path_absent(w + "/home/b/.other/.credentials.json"))
PY
"""


def test_real_symlinks_in_wsl_dangling_ancestor_fails_missing_parent_is_pending(tmp_path):
    done = ti._wsl_root(ANCESTORS, tmp_path)
    out = done.stdout
    assert done.returncode == 0, done.stdout + done.stderr
    assert "DANGLING-PARENT absent=False" in out, out
    assert "MISSING-PARENT absent=True" in out, out
    assert "MISSING-FILE absent=True" in out, out


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 4 - the gate runs UNSANDBOXED (`+`): a sandbox fault must stay loud
# ═══════════════════════════════════════════════════════════════════════════

def test_every_gate_is_prefixed_with_plus():
    for unit, _ in KEEPERS:
        for gate in _exec_conditions((ti.USAGE_DIR / unit).read_text(encoding="utf-8")):
            assert gate.startswith("+/bin/sh -c "), gate
    setup = ti.SETUP_USAGE.read_text(encoding="utf-8")
    assert "KEEPER_GATE='ExecCondition=+/bin/sh -c " in setup


SANDBOX_MATRIX = r"""set -u
U=w7keeper
useradd -m -s /bin/bash $U 2>/dev/null || true
H=$(getent passwd $U | cut -d: -f6)
SD=/run/systemd/system
M=$(mktemp -d -p /srv); chown $U:$U $M; chmod 755 $M      # NOT /tmp: the unit has PrivateTmp=yes
mkdir -p $M/bin; printf '#!/bin/sh\n' > $M/bin/claude; chmod 755 $M/bin/claude
cleanup() {
  for i in $(seq 1 10); do systemctl stop w7case-$i.service >/dev/null 2>&1; rm -rf $SD/w7case-$i.service $SD/w7case-$i.service.d; done
  systemctl daemon-reload; userdel -r $U >/dev/null 2>&1; rm -rf "$M"
}
trap cleanup EXIT
reset() {
  rm -rf $H/.claude $H/.claude.json $M/ran
  mkdir $H/.claude; echo '{}' > $H/.claude/.credentials.json; echo '{}' > $H/.claude.json
  chown -R $U:$U $H/.claude $H/.claude.json; chmod 700 $H/.claude
}
n=0
case_run() {   # name cli
  n=$((n+1))
  sed -e "s|@@CLI@@|$2|g" -e "s|@@HOME@@|$H|g" -e "s|@@MARK@@|$M|g" @@DIR@@/w7-unit.txt > $SD/w7case-$n.service
  mkdir -p $SD/w7case-$n.service.d
  sed -e "s|@@HOME@@|$H|g" -e "s|@@MARK@@|$M|g" @@DIR@@/w7-drop.txt > $SD/w7case-$n.service.d/10-account.conf
  systemctl daemon-reload
  systemctl start w7case-$n.service >/dev/null 2>&1
  ran=0; [ -e $M/ran ] && ran=1
  echo "CASE $1 ran=$ran state=$(systemctl is-failed w7case-$n.service 2>/dev/null)"
}
reset;                                              case_run both          $M/bin/claude
reset;                                              case_run cli_absent    $M/bin/not-installed
reset; rm $H/.claude/.credentials.json;             case_run cred_absent   $M/bin/claude
reset; rm $H/.claude.json;                          case_run json_missing  $M/bin/claude
reset; chown root:root $H/.claude;                  case_run dir_unreadable $M/bin/claude
"""


def test_real_systemd_with_the_shipped_sandbox_a_real_user_and_the_real_read_write_paths(tmp_path):
    """The shipped claude unit (User=, ProtectSystem=strict, ReadWritePaths=, ...) as a
    real unit on the real PID 1: only a genuinely absent CLI or login is skipped; a
    sandbox fault or an unreadable credential directory RUNS and FAILS LOUDLY."""
    unit = (ti.USAGE_DIR / "homehub-claude-refresh@.service").read_text(encoding="utf-8")
    lines = unit.splitlines()
    exec_start = [i for i, l in enumerate(lines) if l.startswith("ExecStart=")]
    assert len(exec_start) == 1
    lines[exec_start[0]] = ("ExecStart=/bin/sh -c 'cat @@HOME@@/.claude/.credentials.json >/dev/null "
                            "&& echo ran > @@MARK@@/ran'")
    unit = "\n".join(lines).replace("/usr/local/bin/claude", "@@CLI@@") + "\n"
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude", "AI_USAGE_USER_ACCOUNT=alice"])
    assert box.run(ti.SETUP_USAGE, STUB_HOME_BASE="/home").returncode == 0
    drop = (box.units / "homehub-claude-refresh@claude.service.d" / "10-account.conf").read_text(encoding="utf-8")
    drop = drop.replace("/home/alice", "@@HOME@@").replace("User=alice", "User=w7keeper") \
               .replace("Group=alice", "Group=w7keeper").rstrip("\n") + "\nReadWritePaths=@@MARK@@\n"
    with open(str(tmp_path / "w7-unit.txt"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(unit)
    with open(str(tmp_path / "w7-drop.txt"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(drop)
    done = ti._wsl_root(SANDBOX_MATRIX, tmp_path)
    if "Failed to connect to bus" in done.stderr or "System has not been booted" in done.stderr:
        pytest.skip("WSL has no running systemd")
    rows = {m[0]: (m[1], m[2]) for m in re.findall(r"CASE (\w+) ran=(\d) state=(\w+)", done.stdout)}
    assert set(rows) == {"both", "cli_absent", "cred_absent", "json_missing", "dir_unreadable"}, (done.stdout, done.stderr)
    assert rows["both"][0] == "1" and rows["both"][1] != "failed", rows
    for skipped in ("cli_absent", "cred_absent"):                      # genuinely absent: quiet
        assert rows[skipped][0] == "0" and rows[skipped][1] != "failed", (skipped, rows)
    # A missing ~/.claude.json (a ReadWritePaths= target) must NOT turn into a quiet skip:
    # the target is `-`-prefixed, so the gate passes and the keeper RUNS.
    assert rows["json_missing"][0] == "1", rows
    # A credential behind a directory the service user cannot enter reads as "there" (the
    # gate is `+`, i.e. root): the keeper runs as the user and FAILS loudly.
    assert rows["dir_unreadable"] == ("0", "failed"), rows
