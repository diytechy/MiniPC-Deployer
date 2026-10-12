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
# Chosen: systemd Condition*= lines, not "enable the timer only once the file exists"
# and not SuccessExitStatus=. A failed condition SKIPS the start (no `failed` state,
# no exit code, nothing to special-case), the timer stays enabled so the first run
# after the sign-in needs no re-run of setup, and it needs no shell exit-code
# convention shared between three programs. SuccessExitStatus= would also hide a
# real exit 3 ("REFUSED") from the Gemini keeper's other causes.

def test_keeper_units_skip_until_the_cli_is_linked_and_the_login_exists(tmp_path):
    box = ti.Box(tmp_path, ti.USAGE_ENV + ["AI_USAGE_SOURCES=claude,claude2,gemini",
                                          "AI_USAGE_USER_ACCOUNT=alice"])
    done = box.run(ti.SETUP_USAGE, STUB_HOME_BASE="/home")
    assert done.returncode == 0, done.stdout + done.stderr
    # The CLI binary condition lives in the shipped unit files, and names the binary
    # the unit really executes (the other side: its ExecStart).
    for unit, binary in (("homehub-claude-refresh@.service", "claude"),
                         ("homehub-gemini-refresh.service", "gemini")):
        text = (ti.USAGE_DIR / unit).read_text(encoding="utf-8")
        directives = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        assert "ConditionFileIsExecutable=/usr/local/bin/%s" % binary in directives, unit
    claude_exec = (ti.USAGE_DIR / "homehub-claude-refresh@.service").read_text(encoding="utf-8")
    assert "exec claude " in claude_exec
    # The credential condition is per-account (drop-in) and equals what sign-in writes:
    # the same mapping the status step checks.
    expect = {"homehub-claude-refresh@claude.service.d": ".claude/.credentials.json",
              "homehub-claude-refresh@claude2.service.d": ".claude-2/.credentials.json",
              "homehub-gemini-refresh.service.d": ".gemini/oauth_creds.json"}
    mapping = {v[1] for v in ti.MAPPINGS.values()}
    for dropin, rel in expect.items():
        assert rel in mapping, rel
        text = (box.units / dropin / "10-account.conf").read_text(encoding="utf-8")
        assert "ConditionPathExists=/home/alice/%s" % rel in text.splitlines(), (dropin, text)


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
    box2 = ti.Box(tmp_path / "ok", [])
    done = _hook_with_usage(box2, tmp_path / "ok", ti.USAGE_ENV + ["AI_USAGE_USER_ACCOUNT=alice"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "provisioner --schedule" in box2.calls
