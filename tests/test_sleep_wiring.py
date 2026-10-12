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
        "setup-sleep.sh", "sleep_feeder.py")}
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
                 "WEIGHT_FEED_URL", "diff -r", "Copy nothing else"):
        assert must in text, must
