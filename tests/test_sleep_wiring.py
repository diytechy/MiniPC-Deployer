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
    assert 'SLEEP_ENABLED="$(_env_val SLEEP_ENABLED)"' in block
    assert "tr -d" not in block        # a matching pair only: tr -d would turn tr'ue into true
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
                                       "SLEEP_ENABLED=yes\n", "SLEEP_ENABLED=tr'ue\n",
                                       "SLEEP_ENABLED='true\n", "SLEEP_ENABLED=true'\n",
                                       'SLEEP_ENABLED=tr"ue\n'])
def test_anything_but_a_literal_true_installs_nothing(tmp_path, env_lines):
    proc, called = run_block(tmp_path, env_lines, stub_body="exit 0")
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout
    assert called is False and "sleep feeder" not in proc.stdout


@needs_bash
@pytest.mark.parametrize("env_lines", ["SLEEP_ENABLED=true\n", 'SLEEP_ENABLED="true"\n',
                                       "SLEEP_ENABLED='true'\n",
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
                 "apply-env-lines.py --restore", "UTF-8 **without a BOM**", "$LASTEXITCODE", "} finally {",
                 "-name '.tmp-.env-*'", "unquoted", "apostrophe", r"scripts\deploy\out\homehub\.env"):
        assert must in text, must
    section = text[text.index("Installing on the RUNNING hub"):text.index("## Tests")]
    assert "tee -a" not in section                 # tee echoes stdin: it would print the password
    assert "not guaranteed" not in section         # compose publishes 127.0.0.1:8787:8787
    assert "sh -c 'cat backup > .env'" in section              # named as the WRONG rollback
    assert "atomic" in section.lower() and "inode buys nothing" in " ".join(section.split())
    assert "cmd /c" in section and "Do not pipe" in section
    assert re.search(r'Do \*\*not\*\* "append\s+what is missing"', section)




# ── TC-1193 : the atomic .env helper (SR-075, LLR-1103) ──────────────────────

import importlib.util
import io
import stat

HELPER = SLEEP / "apply-env-lines.py"
SECRET = "PASSWORD-SENTINEL-7731"


def load_helper():
    spec = importlib.util.spec_from_file_location("apply_env_lines_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Stdin:
    """sys.stdin stand-in with a .buffer, since the helper reads BYTES."""
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def run_helper(tmp_path, monkeypatch, capsys, argv, env_text, stdin=b"", extra=None):
    env = tmp_path / ".env"
    env.write_bytes(env_text.encode("utf-8") if isinstance(env_text, str) else env_text)
    for name, content in (extra or {}).items():
        (tmp_path / name).write_bytes(content)
    monkeypatch.setattr("sys.stdin", Stdin(stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")))
    helper = load_helper()
    try:
        rc = helper.main(argv + ["--env", str(env)])
    except SystemExit as exc:
        rc = exc.code
    out = capsys.readouterr()
    return rc, out.out + out.err, env.read_bytes().decode("utf-8"), env


TEMPLATE_DEFAULTS = ("TIMEZONE=America/Chicago\nSLEEP_ENABLED=false\nSLEEP_USER=\n"
                     "SLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\nMYAIR_EMAIL=\nMYAIR_PASSWORD=\n"
                     "HEART_ENABLED=0\nOTHER=keep\n")
WANTED = ("SLEEP_ENABLED=true\nSLEEP_USER=sub-123\nMYAIR_EMAIL=a@example.invalid\n"  # privacy-ok: placeholder
          "MYAIR_PASSWORD=" + SECRET + "\n")
FULL_GOOD = WANTED + "TIMEZONE=America/Chicago\nSLEEP_FEED_URL=http://127.0.0.1:1/x\n"


def no_temp_left(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []


def test_template_defaults_are_replaced_where_they_stand_and_other_lines_survive(
        tmp_path, monkeypatch, capsys):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, WANTED)
    assert rc == 0 and no_temp_left(tmp_path)
    assert text == ("TIMEZONE=America/Chicago\nSLEEP_ENABLED=true\nSLEEP_USER=sub-123\n"
                    "SLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\nMYAIR_EMAIL=a@example.invalid\n"  # privacy-ok: placeholder
                    "MYAIR_PASSWORD=" + SECRET + "\nHEART_ENABLED=0\nOTHER=keep\n")        # EXACT bytes


def test_shorter_content_leaves_no_stale_tail(tmp_path, monkeypatch, capsys):
    old = ("SLEEP_ENABLED=false\n# " + "x" * 400 + "\nSLEEP_USER=" + "y" * 300 + "\n"
           "SLEEP_USER=" + "z" * 300 + "\nOTHER=1\n")
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], old,
                                  "SLEEP_ENABLED=true\nSLEEP_USER=s\n")
    assert rc == 0 and len(text) < len(old) // 2
    assert text == "SLEEP_ENABLED=true\n# " + "x" * 400 + "\nSLEEP_USER=s\nOTHER=1\n"


def test_absent_keys_are_appended_and_duplicates_collapse_to_one(tmp_path, monkeypatch, capsys):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"],
                                  "SLEEP_ENABLED=false\nOTHER=1\nSLEEP_ENABLED=false\n", WANTED)
    assert rc == 0
    for key in ("SLEEP_ENABLED", "SLEEP_USER", "MYAIR_EMAIL", "MYAIR_PASSWORD"):
        assert len(re.findall(r"(?m)^%s=" % key, text)) == 1, key
    assert "added" in out and "replaced" in out


def test_export_and_indented_copies_of_a_target_key_are_collapsed(tmp_path, monkeypatch, capsys):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"],
                                  "export SLEEP_ENABLED=false\n  SLEEP_USER=old\nOTHER=1\n", WANTED)
    assert rc == 0 and "export" not in text and "  SLEEP_USER" not in text
    assert text.count("SLEEP_ENABLED=") == 1 and text.count("SLEEP_USER=") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_mode_of_the_original_is_preserved(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_bytes(TEMPLATE_DEFAULTS.encode("utf-8"))
    os.chmod(str(env), 0o640)
    monkeypatch.setattr("sys.stdin", Stdin(b"SLEEP_USER=another\n"))
    load_helper().main(["--apply", "--env", str(env)])
    assert stat.S_IMODE(os.stat(str(env)).st_mode) == 0o640 and "another" in env.read_text()


@pytest.mark.parametrize("breaker", ["replace", "write", "fsync"])
def test_a_failure_part_way_leaves_the_original_untouched_and_no_temp_file(
        tmp_path, monkeypatch, capsys, breaker):
    helper = load_helper()

    def boom(*_a, **_k):
        raise OSError("simulated failure")
    if breaker == "replace":
        monkeypatch.setattr(helper.os, "replace", boom)
    elif breaker == "write":
        monkeypatch.setattr(helper, "_write_all", boom)
    else:
        monkeypatch.setattr(helper.os, "fsync", boom)
    env = tmp_path / ".env"
    env.write_bytes(TEMPLATE_DEFAULTS.encode("utf-8"))
    monkeypatch.setattr("sys.stdin", Stdin(WANTED.encode("utf-8")))
    with pytest.raises(OSError):
        helper.main(["--apply", "--env", str(env)])
    assert env.read_bytes() == TEMPLATE_DEFAULTS.encode("utf-8")
    assert no_temp_left(tmp_path)


def test_no_value_is_ever_printed_by_apply_or_report(tmp_path, monkeypatch, capsys):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, WANTED)
    assert SECRET not in out and "sub-123" not in out and "example.invalid" not in out
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"], text)
    assert SECRET not in out and "sub-123" not in out and "example.invalid" not in out
    assert re.search(r"(?m)^SLEEP_ENABLED\s+true$", out) and re.search(r"(?m)^HEART_ENABLED\s+0$", out)
    assert re.search(r"(?m)^MYAIR_PASSWORD\s+set$", out) and rc == 0


def test_report_flags_the_states_that_make_setup_install_nothing(tmp_path, monkeypatch, capsys):
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"], TEMPLATE_DEFAULTS)
    assert rc == 1 and "not the literal true" in out
    assert re.search(r"(?m)^SLEEP_USER\s+empty$", out) and re.search(r"(?m)^MYAIR_PASSWORD\s+empty$", out)
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"],
                                "SLEEP_ENABLED=true\nSLEEP_ENABLED=false\n")
    assert "DUPLICATE KEYS" in out and re.search(r"(?m)^SLEEP_ENABLED\s+false$", out)  # last wins


@pytest.mark.parametrize("quoted", ["''", '""'])
def test_a_quoted_empty_value_is_empty(tmp_path, monkeypatch, capsys, quoted):
    text = FULL_GOOD.replace("MYAIR_PASSWORD=" + SECRET, "MYAIR_PASSWORD=" + quoted) + "HEART_ENABLED=0\n"
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"], text)
    assert rc == 1 and re.search(r"(?m)^MYAIR_PASSWORD\s+empty$", out)


@pytest.mark.parametrize("heart", ["HEART_ENABLED=1\n", "HEART_ENABLED=true\n", ""])
def test_report_fails_unless_heart_is_literally_zero(tmp_path, monkeypatch, capsys, heart):
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"], FULL_GOOD + heart)
    assert rc == 1 and "HEART_ENABLED must be the literal 0" in out
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"], FULL_GOOD + "HEART_ENABLED=0\n")
    assert rc == 0


def test_report_flags_export_and_indented_forms(tmp_path, monkeypatch, capsys):
    rc, out, _t, _ = run_helper(tmp_path, monkeypatch, capsys, ["--report"],
                                "export SLEEP_ENABLED=true\n  MYAIR_EMAIL=x\n")
    assert rc == 1 and "UNSUPPORTED FORM" in out and "SLEEP_ENABLED" in out


@pytest.mark.parametrize("bad", ["SLEEP_MODE=x\n", "SLEEP_START=1\n", "SLEEP_USR=1\n", "PATH=/x\n",
                                 "NOEQUALS-" + SECRET + "\n", "SLEEP_ENABLED\n",
                                 "MYAIR_PASSWORD=\n", "MYAIR_PASSWORD=''\n", 'SLEEP_USER=""\n',
                                 "SLEEP_ENABLED=\n"])
def test_a_bad_or_blanking_line_is_refused_with_the_file_untouched_and_nothing_echoed(
        tmp_path, monkeypatch, capsys, bad):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS,
                                  "SLEEP_FEED_URL=http://127.0.0.1:1/y\n" + bad)
    assert rc == 2 and text == TEMPLATE_DEFAULTS and "REFUSED" in out
    assert SECRET not in out and no_temp_left(tmp_path)


def test_a_blank_optional_value_is_allowed(tmp_path, monkeypatch, capsys):
    rc, _o, text, _e = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS,
                                  "SLEEP_FEED_TOKEN=\n")
    assert rc == 0 and "SLEEP_FEED_TOKEN=\n" in text


def test_non_utf8_and_a_bom_on_stdin_are_refused(tmp_path, monkeypatch, capsys):
    for payload in (b"\xef\xbb\xbfSLEEP_USER=a\n", b"SLEEP_USER=caf\xe9\n"):
        rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, payload)
        assert rc == 2 and text == TEMPLATE_DEFAULTS


def test_utf8_bytes_round_trip_exactly(tmp_path, monkeypatch, capsys):
    password = "päss€wörd"
    line = ("MYAIR_PASSWORD=" + password + "\n").encode("utf-8")
    rc, _o, _text, env = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, line)
    assert rc == 0 and line in env.read_bytes()


def test_a_missing_env_is_refused_rather_than_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", Stdin(WANTED.encode("utf-8")))
    with pytest.raises(SystemExit) as caught:
        load_helper().main(["--apply", "--env", str(tmp_path / "nope")])
    assert caught.value.code == 2 and not (tmp_path / "nope").exists()


# restore: validated first, then the same atomic writer

def test_restore_replaces_the_env_with_the_backup_exactly(tmp_path, monkeypatch, capsys):
    backup = "A=1\nSLEEP_ENABLED=false\n"
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys,
                                  ["--restore", str(tmp_path / ".env.bak-1")],
                                  TEMPLATE_DEFAULTS + "MORE=" + "q" * 500 + "\n",
                                  extra={".env.bak-1": backup.encode("utf-8")})
    assert rc == 0 and text == backup and no_temp_left(tmp_path)


@pytest.mark.parametrize("case", ["missing", "empty", "blank", "directory", "same", "binary"])
def test_restore_validates_the_backup_before_touching_the_env(tmp_path, monkeypatch, capsys, case):
    extra, name = {}, ".env.bak-x"
    if case == "empty":
        extra[name] = b""
    elif case == "blank":
        extra[name] = b"\n  \n"
    elif case == "binary":
        extra[name] = b"\xff\xfe\x00bad"
    elif case == "directory":
        (tmp_path / name).mkdir()
    elif case == "same":
        name = ".env"
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--restore", str(tmp_path / name)],
                                  TEMPLATE_DEFAULTS, extra=extra)
    assert rc == 2 and "REFUSED" in out
    assert text == TEMPLATE_DEFAULTS and no_temp_left(tmp_path)


def test_a_failed_restore_leaves_the_env_alone(tmp_path, monkeypatch, capsys):
    helper = load_helper()

    def boom(*_a, **_k):
        raise OSError("simulated failure")
    monkeypatch.setattr(helper.os, "replace", boom)
    env = tmp_path / ".env"
    env.write_bytes(TEMPLATE_DEFAULTS.encode("utf-8"))
    (tmp_path / "b").write_bytes(b"K=1\n")
    with pytest.raises(OSError):
        helper.main(["--restore", str(tmp_path / "b"), "--env", str(env)])
    assert env.read_bytes() == TEMPLATE_DEFAULTS.encode("utf-8") and no_temp_left(tmp_path)


def test_the_helper_is_lf_with_a_shebang():
    raw = HELPER.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in raw


def test_mode_and_owner_of_the_original_are_applied_to_the_replacement(tmp_path, monkeypatch, capsys):
    # Portable: record the calls (Windows has no fchmod/fchown), assert the ORIGINAL's values.
    helper = load_helper()
    calls = []
    monkeypatch.setattr(helper.os, "fchmod", lambda fd, mode: calls.append(("mode", mode)), raising=False)
    monkeypatch.setattr(helper.os, "fchown", lambda fd, uid, gid: calls.append(("owner", uid, gid)), raising=False)
    env = tmp_path / ".env"
    env.write_bytes(TEMPLATE_DEFAULTS.encode("utf-8"))
    before = os.stat(str(env))
    monkeypatch.setattr("sys.stdin", Stdin(b"SLEEP_USER=another\n"))
    assert helper.main(["--apply", "--env", str(env)]) == 0
    assert ("mode", before.st_mode & 0o7777) in calls
    assert ("owner", before.st_uid, before.st_gid) in calls


# ── Round 3 : quoting vs the REAL gate, durability warning, stale temps, locking ──

import subprocess as _sp
import sys
import time

from test_sleep_deploy import GOOD_ENV, run_setup


@needs_bash
@pytest.mark.parametrize("raw,installs", [
    ("true", True), ("'true'", True), ('"true"', True),
    ("false", False), ("'false'", False), ("'True'", False), ("'1'", False), ("''", False),
    ("tr'ue", False), ("'true", False), ("true'", False), ('tr"ue', False), ("\"true'", False)])
def test_the_real_setup_gate_reads_single_double_and_bare_values_alike(tmp_path, raw, installs):
    env_text = GOOD_ENV.replace("SLEEP_ENABLED=true", "SLEEP_ENABLED=" + raw)
    proc = run_setup(tmp_path, ["--check-only"], env_text)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    if installs:
        assert "check-only: identity set" in out
    else:
        assert "SLEEP_ENABLED is not true" in out


@needs_bash
def test_single_quoted_empty_sign_in_is_blank_to_the_real_script(tmp_path):
    env_text = GOOD_ENV.replace("MYAIR_PASSWORD=PASSWORD-SENTINEL-6120", "MYAIR_PASSWORD=''")
    proc = run_setup(tmp_path, ["--check-only"], env_text)
    assert proc.returncode == 2 and "MYAIR_PASSWORD is blank" in proc.stdout + proc.stderr


@needs_bash
@pytest.mark.parametrize("quoted", ["'true'", '"true"'])
def test_what_apply_writes_passes_the_real_gate_even_from_quoted_input(
        tmp_path, monkeypatch, capsys, quoted):
    incoming = ("SLEEP_ENABLED=" + quoted + "\nSLEEP_USER=sub-1\nMYAIR_EMAIL=a@example.invalid\n"  # privacy-ok: placeholder
                "MYAIR_PASSWORD=" + SECRET + "\nSLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\n"
                "TIMEZONE=America/Chicago\nHEART_ENABLED='0'\n")
    rc, _o, text, _e = run_helper(tmp_path, monkeypatch, capsys, ["--apply"],
                                  "SLEEP_ENABLED=false\n", incoming)
    assert rc == 0 and "SLEEP_ENABLED=true\n" in text and "HEART_ENABLED=0\n" in text
    assert "'" not in text.replace(SECRET, "")
    proc = run_setup(tmp_path, ["--check-only"], text)
    assert proc.returncode == 0 and "check-only: identity set" in proc.stdout


@pytest.mark.parametrize("raw,canonical", [("'true'", "true"), ('"false"', "false"),
                                           ("'1'", "1"), ('"0"', "0"), ("true", "true")])
def test_flag_keys_are_written_unquoted(tmp_path, monkeypatch, capsys, raw, canonical):
    rc, _o, text, _e = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], "OTHER=1\n",
                                  "SLEEP_ENABLED=%s\nHEART_ENABLED=%s\nSLEEP_PUBLISH=%s\n" % (raw, raw, raw))
    assert rc == 0
    for key in ("SLEEP_ENABLED", "HEART_ENABLED", "SLEEP_PUBLISH"):
        assert "%s=%s\n" % (key, canonical) in text


def test_a_directory_fsync_failure_after_the_replace_is_a_warning_not_a_failure(
        tmp_path, monkeypatch, capsys):
    helper = load_helper()

    def boom(_directory):
        raise OSError("simulated directory fsync failure")
    monkeypatch.setattr(helper, "_fsync_dir", boom)
    env = tmp_path / ".env"
    env.write_bytes(TEMPLATE_DEFAULTS.encode("utf-8"))
    monkeypatch.setattr("sys.stdin", Stdin(WANTED.encode("utf-8")))
    rc = helper.main(["--apply", "--env", str(env)])
    out = capsys.readouterr()
    assert rc == 0
    assert "SLEEP_USER=sub-123\n" in env.read_text(encoding="utf-8")        # the change IS in place
    assert "WARNING" in out.err and "durability is uncertain" in out.err
    assert SECRET not in out.out + out.err and no_temp_left(tmp_path)


def test_stale_temp_files_from_a_killed_run_are_swept_under_the_lock(tmp_path, monkeypatch, capsys):
    (tmp_path / ".tmp-.env-abc12345").write_bytes(b"MYAIR_PASSWORD=" + SECRET.encode("utf-8") + b"\n")
    (tmp_path / "unrelated.txt").write_bytes(b"keep")
    rc, out, _text, _e = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, WANTED)
    assert rc == 0 and no_temp_left(tmp_path) and (tmp_path / "unrelated.txt").exists()
    assert "removed 1 stale .tmp-.env-* file" in out and SECRET not in out


def test_report_does_not_touch_the_directory(tmp_path, monkeypatch, capsys):
    (tmp_path / ".tmp-.env-abc12345").write_bytes(b"x")
    run_helper(tmp_path, monkeypatch, capsys, ["--report"], TEMPLATE_DEFAULTS)
    assert (tmp_path / ".tmp-.env-abc12345").exists()


# Locking. Linux only: flock does not exist on Windows. These fail if the lock or the
# inode re-check is removed (mutation-checked).

linux_only = pytest.mark.skipif(os.name == "nt", reason="flock is POSIX")


def _spawn(env, stdin_text):
    proc = _sp.Popen([sys.executable, str(HELPER), "--apply", "--env", str(env)],
                     stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE)
    proc.stdin.write(stdin_text.encode("utf-8"))
    proc.stdin.close()
    return proc


@linux_only
def test_two_helpers_wait_for_the_lock_and_neither_update_is_lost(tmp_path):
    import fcntl
    env = tmp_path / ".env"
    env.write_bytes(b"OTHER=1\n")
    holder = open(str(env), "rb")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        first = _spawn(env, "SLEEP_USER=from-first\n")
        second = _spawn(env, "MYAIR_EMAIL=from-second\n")
        time.sleep(1.0)
        assert first.poll() is None and second.poll() is None     # both BLOCKED on the lock
        assert env.read_bytes() == b"OTHER=1\n"
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    for proc in (first, second):
        assert proc.wait(timeout=30) == 0, proc.stderr.read()
    text = env.read_text(encoding="utf-8")
    assert "SLEEP_USER=from-first\n" in text and "MYAIR_EMAIL=from-second\n" in text
    assert text.startswith("OTHER=1\n") and no_temp_left(tmp_path)


@linux_only
def test_a_waiter_re_reads_a_file_replaced_while_it_waited(tmp_path):
    import fcntl
    helper = load_helper()
    env = tmp_path / ".env"
    env.write_bytes(b"OTHER=1\n")
    holder = open(str(env), "rb")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)      # the "first helper"
    waiter = _spawn(env, "SLEEP_USER=from-waiter\n")
    try:
        time.sleep(1.0)
        assert waiter.poll() is None
        helper.atomic_write(str(env), b"OTHER=1\nWRITTEN_BY_FIRST=1\n")   # new inode under the waiter
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert waiter.wait(timeout=30) == 0, waiter.stderr.read()
    text = env.read_text(encoding="utf-8")
    assert "WRITTEN_BY_FIRST=1\n" in text and "SLEEP_USER=from-waiter\n" in text   # nothing lost


# ── Round 4 : temp names scoped to the target; one quote rule; password characters ──

def test_one_helpers_sweep_never_touches_another_env_files_live_temp(tmp_path, monkeypatch, capsys):
    other_live = tmp_path / ".tmp-other.env-q1w2e3r4"          # a different target's in-flight temp
    other_live.write_bytes(b"IN-FLIGHT")
    ours_stale = tmp_path / ".tmp-.env-a1b2c3d4"
    ours_stale.write_bytes(b"STALE")
    lookalike = tmp_path / ".tmp-.env-a-b-q1w2e3r4"             # another target whose name starts like ours
    lookalike.write_bytes(b"OTHER")
    rc, out, _t, _e = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, WANTED)
    assert rc == 0 and not ours_stale.exists()
    assert other_live.read_bytes() == b"IN-FLIGHT" and lookalike.read_bytes() == b"OTHER"


def test_the_temp_prefix_is_a_function_of_the_target_name():
    helper = load_helper()
    assert helper.temp_prefix("/x/.env") != helper.temp_prefix("/x/other.env")
    assert helper.temp_prefix("/x/.env") == helper.temp_prefix("/y/.env")


def test_a_live_temp_of_another_target_survives_a_real_apply_on_this_one(tmp_path, monkeypatch, capsys):
    helper = load_helper()
    seen = []
    real = helper.os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        return real(src, dst)
    monkeypatch.setattr(helper.os, "replace", spy)
    env = tmp_path / "other.env"
    env.write_bytes(b"OTHER=1\n")
    monkeypatch.setattr("sys.stdin", Stdin(b"SLEEP_USER=a\n"))
    assert helper.main(["--apply", "--env", str(env)]) == 0
    assert seen and seen[0].startswith(".tmp-other.env-")


@pytest.mark.parametrize("line", ["SLEEP_ENABLED='true\n", 'SLEEP_USER="abc\n', "SLEEP_USER='abc\n"])
def test_a_value_with_an_unmatched_leading_quote_is_refused(tmp_path, monkeypatch, capsys, line):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS, line)
    assert rc == 2 and text == TEMPLATE_DEFAULTS and "no matching closing quote" in out


@pytest.mark.parametrize("password,ok", [
    ("pa'ss", True), ("pass'", True), ("pa'ss'word", True), ("pa$$word", True),
    ("'pass", False), ("'pass'", False), ('"pass"', False), ('pa"ss', False), ("pa\\ss", False)])
def test_password_characters_systemd_would_alter_are_refused(tmp_path, monkeypatch, capsys, password, ok):
    rc, out, text, _ = run_helper(tmp_path, monkeypatch, capsys, ["--apply"], TEMPLATE_DEFAULTS,
                                  "MYAIR_PASSWORD=" + password + "\n")
    assert (rc == 0) is ok
    assert (("MYAIR_PASSWORD=" + password + "\n") in text) is ok
    assert password not in out
