"""TC-1190..TC-1192 - the sleep feeder's DEPLOY wiring (SR-074, SR-075).

The feeder, its unit and its setup script were built and tested in isolation
(test_sleep_deploy.py); this file is about whether anything CARRIES them to a box:

  * stack/sleep/ is tracked, so the payload `git archive` carries it to the ISO
    (TC-1190);
  * firstboot.sh calls setup-sleep.sh under the literal SLEEP_ENABLED=true gate,
    and the block is RUN against stubs - not grepped - in every branch that
    matters: off, on, refused, and script missing (TC-1191);
  * the heart gauge needs no hook of its own because it rides the weight feeder,
    which reads the whole .env through EnvironmentFile= (TC-1192).

Verifies: TC-1190, TC-1191, TC-1192 (SR-074, SR-075, LLR-1100..LLR-1103)
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIRSTBOOT = REPO / "stack" / "autoinstall" / "firstboot.sh"
SLEEP = REPO / "stack" / "sleep"
WEIGHT = REPO / "stack" / "weight"
ENV_EXAMPLE = REPO / "stack" / ".env.example"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

START = "# ── 6f-2. the sleep feeder"
END = "# ── 6g. hub recovery"


def sleep_block():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1
    a, b = text.index(START), text.index(END)
    assert a < b
    return text[a:b]


# ── TC-1190 : the payload carries the tree ───────────────────────────────────

def test_every_file_the_unit_needs_is_tracked_so_the_payload_archive_carries_it():
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("no git checkout to ask")
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "stack/sleep"],
                         capture_output=True, text=True, check=True).stdout.split()
    wanted = {"stack/sleep/" + n for n in (
        "README.md", "homehub-sleep.service", "homehub-sleep.timer",
        "setup-sleep.sh", "sleep_feeder.py", "apply-env-lines.py")}
    assert wanted <= set(out), sorted(wanted - set(out))


def test_the_payload_is_the_whole_repo_not_a_list_of_directories():
    # If someone narrows the payload to a named list, sleep/ must be on it.
    text = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "vmtest" / "lib").glob("*.sh"))
    assert "archive --format=tar HEAD" in text
    user_data = (REPO / "stack" / "autoinstall" / "user-data").read_text(encoding="utf-8")
    assert 'cp -a "$d/deploy-payload/." /target/opt/homehub/' in user_data


# ── TC-1191 : the firstboot hook, run ────────────────────────────────────────

def test_the_hook_sits_after_weight_before_recovery_and_names_the_script():
    block = sleep_block()
    text = FIRSTBOOT.read_text(encoding="utf-8")
    assert text.index("# ── 6f. the weight feeder") < text.index(START)
    assert "$STACK_DIR/sleep/setup-sleep.sh" in block
    assert 'SLEEP_ENABLED="$(_env_val SLEEP_ENABLED | tr -d \'"\')"' in block
    assert '[ "${SLEEP_ENABLED:-false}" = "true" ]' in block


def run_block(tmp_path, env_lines, stub_body=None):
    stack = tmp_path / "stack"
    (stack / "sleep").mkdir(parents=True)
    (stack / ".env").write_bytes(env_lines.encode("utf-8"))
    marker = tmp_path / "called"
    if stub_body is not None:
        (stack / "sleep" / "setup-sleep.sh").write_bytes(
            ("#!/usr/bin/env bash\necho called > '%s'\n%s\n" % (
                str(marker).replace("\\", "/"), stub_body)).encode("utf-8"))
    harness = (
        "set -euo pipefail\n"
        "STACK_DIR='%s'\n"
        "log() { printf '%%s\\n' \"$*\"; }\n"
        "_env_val() { sed -n \"s/^$1=//p\" \"$STACK_DIR/.env\" 2>/dev/null | head -1 | tr -d '\\r'; }\n"
        % str(stack).replace("\\", "/")) + sleep_block() + "\necho BLOCK-DONE\n"
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                          env=dict(os.environ))
    return proc, marker.exists()


@needs_bash
@pytest.mark.parametrize("env_lines", ["", "SLEEP_ENABLED=false\n", "SLEEP_ENABLED=\n",
                                       "SLEEP_ENABLED=True\n", "SLEEP_ENABLED=1\n",
                                       "SLEEP_ENABLED=yes\n"])
def test_anything_but_a_literal_true_installs_nothing(tmp_path, env_lines):
    proc, called = run_block(tmp_path, env_lines, stub_body="exit 0")
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout
    assert called is False and "sleep feeder" not in proc.stdout


@needs_bash
@pytest.mark.parametrize("env_lines", ["SLEEP_ENABLED=true\n", 'SLEEP_ENABLED="true"\n',
                                       "SLEEP_ENABLED=true\r\n"])
def test_a_true_gate_runs_the_setup_script_and_says_so(tmp_path, env_lines):
    proc, called = run_block(tmp_path, env_lines, stub_body="echo stub-installed; exit 0")
    assert proc.returncode == 0 and called is True
    assert "sleep feeder installed" in proc.stdout and "WARNING" not in proc.stdout


@needs_bash
def test_a_refusal_is_a_warning_and_never_a_failed_first_boot(tmp_path):
    proc, called = run_block(tmp_path, "SLEEP_ENABLED=true\n",
                             stub_body="echo REFUSED: MYAIR_PASSWORD is blank; exit 2")
    assert called is True
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout     # pipefail + set -e survived
    assert "WARNING: setup-sleep.sh refused or failed" in proc.stdout
    assert "sleep feeder installed" not in proc.stdout
    assert "REFUSED: MYAIR_PASSWORD is blank" in proc.stdout        # the reason is passed through


@needs_bash
def test_an_enabled_box_with_no_script_on_the_payload_says_carriage_is_missing(tmp_path):
    proc, called = run_block(tmp_path, "SLEEP_ENABLED=true\n", stub_body=None)
    assert called is False and proc.returncode == 0
    assert "carriage missing" in proc.stdout


@needs_bash
def test_firstboot_still_parses():
    proc = subprocess.run(["bash", "-n", str(FIRSTBOOT).replace("\\", "/")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ── TC-1192 : heart rides the weight feeder ──────────────────────────────────

def test_the_weight_unit_hands_the_whole_env_to_the_feeder_so_heart_needs_no_hook():
    unit = (WEIGHT / "homehub-weight.service").read_text(encoding="utf-8")
    assert re.search(r"(?m)^EnvironmentFile=/opt/homehub/stack/\.env$", unit)
    # ...and the setup script neither filters the environment nor reads HEART_*.
    setup = (WEIGHT / "setup-weight.sh").read_text(encoding="utf-8")
    assert "HEART_" not in setup and "env -i" not in setup


def test_the_example_ships_heart_off_with_the_documented_bounds_and_sleep_off():
    values = dict(re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert values["HEART_ENABLED"] == "0"
    assert (values["HEART_MIN_BPM"], values["HEART_MAX_BPM"], values["HEART_TARGET_BPM"]) == \
        ("40", "100", "60")
    assert values["SLEEP_ENABLED"] == "false" and values["SLEEP_USER"] == ""


def test_the_readme_carries_the_running_hub_procedure_and_no_stale_claim():
    text = (SLEEP / "README.md").read_text(encoding="utf-8")
    assert "does **not** yet call it" not in text
    for must in ("Installing on the RUNNING hub", "not a git checkout", "systemctl list-timers",
                 "journalctl -u homehub-sleep", "setup-sleep.sh --check-only",
                 "apply-env-lines.py --apply", "apply-env-lines.py --report", "diff -r",
                 "Copy nothing else", "systemctl stop homehub-sleep.service",
                 "cat /opt/homehub/stack/.env.bak-<stamp> > /opt/homehub/stack/.env"):
        assert must in text, must
    section = text[text.index("Installing on the RUNNING hub"):text.index("## Tests")]
    assert "tee -a" not in section                 # tee echoes stdin: it would print the password
    assert "not guaranteed" not in section         # compose publishes 127.0.0.1:8787:8787
    assert "--remove-destination" in section and "sed -i" in section   # named as the WRONG forms
    assert re.search(r'Do \*\*not\*\* "append\s+what is missing"', section)


# ── TC-1193 : the in-place .env helper (SR-075, LLR-1103) ────────────────────

import importlib.util
import io

HELPER = SLEEP / "apply-env-lines.py"
SECRET = "PASSWORD-SENTINEL-7731"


def load_helper():
    spec = importlib.util.spec_from_file_location("apply_env_lines_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_helper(tmp_path, monkeypatch, capsys, mode, env_text, stdin_text=""):
    env = tmp_path / ".env"
    env.write_bytes(env_text.encode("utf-8"))
    before = os.stat(str(env)).st_ino
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    helper = load_helper()
    try:
        rc = helper.main([mode, "--env", str(env)])
    except SystemExit as exc:
        rc = exc.code
    out = capsys.readouterr()
    return rc, out.out + out.err, env.read_bytes().decode("utf-8"), before == os.stat(str(env)).st_ino


TEMPLATE_DEFAULTS = ("TIMEZONE=America/Chicago\nSLEEP_ENABLED=false\nSLEEP_USER=\n"
                     "SLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\nMYAIR_EMAIL=\nMYAIR_PASSWORD=\n"
                     "HEART_ENABLED=0\nOTHER=keep\n")
WANTED = ("SLEEP_ENABLED=true\nSLEEP_USER=sub-123\nMYAIR_EMAIL=a@example.invalid\n"  # privacy-ok: placeholder
          "MYAIR_PASSWORD=" + SECRET + "\n")


def test_template_defaults_are_replaced_in_place_not_left_standing(tmp_path, monkeypatch, capsys):
    rc, out, text, same_inode = run_helper(tmp_path, monkeypatch, capsys, "--apply",
                                           TEMPLATE_DEFAULTS, WANTED)
    assert rc == 0 and same_inode is True
    assert "SLEEP_ENABLED=true\n" in text and "SLEEP_ENABLED=false" not in text
    assert "MYAIR_PASSWORD=" + SECRET + "\n" in text
    assert text.count("SLEEP_ENABLED=") == 1 and "OTHER=keep\n" in text
    assert "SLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\n" in text      # untouched key kept
    assert text.index("SLEEP_ENABLED=true") < text.index("SLEEP_USER=")   # replaced where it was


def test_absent_keys_are_appended_and_duplicates_collapse_to_one(tmp_path, monkeypatch, capsys):
    rc, out, text, same_inode = run_helper(
        tmp_path, monkeypatch, capsys, "--apply",
        "SLEEP_ENABLED=false\nOTHER=1\nSLEEP_ENABLED=false\n", WANTED)
    assert rc == 0 and same_inode is True
    for key in ("SLEEP_ENABLED", "SLEEP_USER", "MYAIR_EMAIL", "MYAIR_PASSWORD"):
        assert len(re.findall(r"(?m)^%s=" % key, text)) == 1, key
    assert "added" in out and "replaced" in out


def test_no_value_is_ever_printed_by_apply_or_report(tmp_path, monkeypatch, capsys):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, "--apply", TEMPLATE_DEFAULTS, WANTED)
    assert SECRET not in out and "sub-123" not in out and "example.invalid" not in out
    rc, out, _text, _ = run_helper(tmp_path, monkeypatch, capsys, "--report", text)
    assert SECRET not in out and "sub-123" not in out and "example.invalid" not in out
    assert re.search(r"(?m)^SLEEP_ENABLED\s+true$", out)            # the flag's literal value
    assert re.search(r"(?m)^HEART_ENABLED\s+0$", out)
    assert re.search(r"(?m)^MYAIR_PASSWORD\s+set$", out) and rc == 0


def test_report_flags_the_states_that_make_setup_install_nothing(tmp_path, monkeypatch, capsys):
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, "--report", TEMPLATE_DEFAULTS)
    assert rc == 1
    assert re.search(r"(?m)^SLEEP_USER\s+empty$", out) and re.search(r"(?m)^MYAIR_PASSWORD\s+empty$", out)
    assert "not the literal true" in out
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, "--report",
                                "SLEEP_ENABLED=true\nSLEEP_ENABLED=false\n")
    assert "DUPLICATE KEYS" in out and re.search(r"(?m)^SLEEP_ENABLED\s+false$", out)  # last wins


@pytest.mark.parametrize("bad", ["SLEEP_MODE=x\n", "SLEEP_START=1\n", "PATH=/x\n",
                                 "NOEQUALS-" + SECRET + "\n", "SLEEP_ENABLED\n"])
def test_a_bad_line_is_refused_with_the_file_untouched_and_nothing_echoed(
        tmp_path, monkeypatch, capsys, bad):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, "--apply", TEMPLATE_DEFAULTS,
                                  "SLEEP_ENABLED=true\n" + bad)
    assert rc == 2 and text == TEMPLATE_DEFAULTS and "REFUSED" in out
    assert SECRET not in out


def test_a_missing_env_is_refused_rather_than_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(WANTED))
    helper = load_helper()
    with pytest.raises(SystemExit) as caught:
        helper.main(["--apply", "--env", str(tmp_path / "nope")])
    assert caught.value.code == 2 and not (tmp_path / "nope").exists()


def test_the_helper_is_lf_with_a_shebang_and_tracked():
    raw = HELPER.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in raw
