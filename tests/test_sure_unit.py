"""Sure is owned by its own unit and switched by SURE_ENABLED, never COMPOSE_PROFILES.

The 2026-10-18 Sure deploy review found that naming `sure` in COMPOSE_PROFILES
makes firstboot's every-boot bulk `docker compose up -d` wait on Sure's
database and cache before DNS, Samba and recovery. The hub's config dropped the
profile that day and Sure was started by hand, which left three gaps, each
asserted here against the code that closes it:

  * nothing owned Sure: homehub-sure.service + stack/sure/sure-unit.sh now do,
    on the crossplay relay's knob-plus-unit pattern (TC-1360);
  * firstboot installed nothing for it: its 4-pre-a2b block now installs the
    unit on every boot, enables it and QUEUES its start when the knob is
    exactly `true`, and turns it off only when it had owned Sure (TC-1361);
  * the bake and the image resolver keyed on the profile, so with the profile
    gone the fork (sure-auroleap:local, no registry home) was silently left off
    every ISO - that is in tests/test_sure_image_bake.py, TC-1362.

Behaviour is EXECUTED: the real script runs against a PATH stub `docker`, and
the real firstboot block is cut out of firstboot.sh and run in bash with
functions standing in for systemctl and install.

Verifies: TC-1360, TC-1361 (SR-100, LLR-1210)
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SURE = ROOT / "stack" / "sure"
SCRIPT = SURE / "sure-unit.sh"
UNIT = SURE / "homehub-sure.service"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"
ENVEX = ROOT / "stack" / ".env.example"

FOUR = ["sure-postgres", "sure-redis", "sure-web", "sure-worker"]


def _w(path, text):
    """LF bytes, whatever the platform."""
    Path(path).write_bytes(text.encode("utf-8"))


def _px(path):
    """A path Git Bash understands on Windows (/c/...), unchanged elsewhere."""
    text = Path(path).as_posix()
    if len(text) > 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _bash():
    git = Path("C:/Program Files/Git/bin/bash.exe")
    if git.exists():
        return str(git)
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("bash not available")
    return exe


def _live(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _unit_values(key):
    return [l.split("=", 1)[1].strip() for l in _live(UNIT.read_text(encoding="utf-8")).splitlines()
            if l.startswith(key + "=")]


# ── TC-1360: the unit and its script ─────────────────────────────────────────

DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
[ -n "${DOCKER_FAIL:-}" ] && exit 1
exit 0
"""


def _run_script(tmp_path, verb, env_text="SURE_ENABLED=true\n", fail=False):
    stack = tmp_path / "stack"
    stack.mkdir(exist_ok=True)
    _w(stack / ".env", env_text)
    docker = tmp_path / "docker"
    _w(docker, DOCKER_STUB)
    docker.chmod(0o755)
    calls = tmp_path / "calls.txt"
    _w(calls, "")
    env = dict(os.environ, STACK_DIR=_px(stack), DOCKER=_px(docker), CALLS=_px(calls))
    if fail:
        env["DOCKER_FAIL"] = "1"
    proc = subprocess.run([_bash(), _px(SCRIPT), verb], env=env, capture_output=True, text=True)
    return proc, calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("env_text,expected", [
    ("SURE_ENABLED=true\n", 0),
    ('SURE_ENABLED="true"\n', 0),
    ("SURE_ENABLED=true\r\n", 0),
    ("SURE_ENABLED=false\nSURE_ENABLED=true\n", 0),   # the last assignment wins
    ("SURE_ENABLED=true\nSURE_ENABLED=false\n", 1),
    ("SURE_ENABLED=false\n", 1),
    ("SURE_ENABLED=True\n", 1),                        # exactly `true`, as the backup rows read it
    ("SURE_ENABLED=yes\n", 1),
    ("SURE_ENABLED=\n", 1),
    ("COMPOSE_PROFILES=sure\n", 1),                    # the profile is not the switch
])
def test_tc1360_condition_is_exactly_true_sr100(tmp_path, env_text, expected):
    proc, calls = _run_script(tmp_path, "condition", env_text)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    assert calls == [], "the condition must not call docker: %r" % calls
    if expected:
        assert "not a failure" in proc.stdout


def test_tc1360_start_is_the_four_services_no_build_no_pull_sr100(tmp_path):
    proc, calls = _run_script(tmp_path, "start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == ["compose --profile sure up -d --no-build --pull never " + " ".join(FOUR)]


def test_tc1360_stop_takes_web_and_worker_before_their_database_sr100(tmp_path):
    proc, calls = _run_script(tmp_path, "stop")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == ["compose --profile sure stop -t 60 sure-web sure-worker sure-redis sure-postgres"]


@pytest.mark.parametrize("verb", ["start", "stop"])
def test_tc1360_a_failed_compose_fails_the_unit_sr100(tmp_path, verb):
    proc, _ = _run_script(tmp_path, verb, fail=True)
    assert proc.returncode == 1
    assert "FATAL" in proc.stdout


def test_tc1360_an_unknown_verb_is_a_usage_error_sr100(tmp_path):
    proc, calls = _run_script(tmp_path, "restart")
    assert proc.returncode == 2 and calls == []


def test_tc1360_unit_shape_sr100():
    """Wants= (not Requires=) docker: a needrestart docker.service restart must
    not propagate a STOP that leaves Sure user-stopped until the next boot."""
    live = _live(UNIT.read_text(encoding="utf-8"))
    assert "Requires=" not in live, "a docker restart would stop Sure and nothing would start it again"
    assert "docker.service" in " ".join(_unit_values("Wants"))
    after = " ".join(_unit_values("After"))
    assert "docker.service" in after and "homehub-firstboot.service" in after
    assert "BindsTo=" not in live and "PartOf=" not in live
    assert _unit_values("Type") == ["oneshot"] and _unit_values("RemainAfterExit") == ["yes"]
    script = "/opt/homehub/stack/sure/sure-unit.sh"
    assert _unit_values("ExecCondition") == ["/bin/bash %s condition" % script]
    assert _unit_values("ExecStart") == ["/bin/bash %s start" % script]
    assert _unit_values("ExecStop") == ["/bin/bash %s stop" % script]
    assert _unit_values("WantedBy") == ["multi-user.target"]
    # four 60 s stops in dependency order must fit the stop budget
    assert int(_unit_values("TimeoutStopSec")[0]) >= 4 * 60


def test_tc1360_the_services_keep_unless_stopped_sr100():
    import yaml
    services = yaml.safe_load((ROOT / "stack" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for name in FOUR:
        assert services[name]["restart"] == "unless-stopped", name
        assert services[name]["profiles"] == ["sure"], name


def test_tc1360_files_are_lf_sr100():
    for path in (SCRIPT, UNIT):
        assert b"\r" not in path.read_bytes(), path


# ── TC-1361: firstboot's block, executed ─────────────────────────────────────

def _sure_block():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    a = text.index("# ── 4-pre-a2b. SURE")
    b = text.index("# ── 4-pre-a3.", a)
    return text[a:b]


_PREAMBLE = r'''
set -euo pipefail
log() { echo "[firstboot] $*"; }
systemctl() {
    echo "systemctl $*" >> "$CALLS"
    if [ "$1" = is-enabled ]; then [ -n "${STUB_ENABLED:-}" ]; return; fi
    if [ "${STUB_SYSTEMCTL_FAIL:-}" = "$1" ]; then return 1; fi
    return 0
}
install() { echo "install $*" >> "$CALLS"; return 0; }
'''


def _run_block(tmp_path, env_text, enabled=False, fail=None, payload=True):
    stack = tmp_path / "stack"
    (stack / "sure").mkdir(parents=True, exist_ok=True)
    if payload:
        shutil.copy(UNIT, stack / "sure" / UNIT.name)
    _w(stack / ".env", env_text)
    calls = tmp_path / "calls.txt"
    _w(calls, "")
    script = tmp_path / "block.sh"
    _w(script, _PREAMBLE + 'STACK_DIR="%s"\n' % _px(stack) + _sure_block() + "\necho BLOCK-DONE\n")
    env = dict(os.environ, CALLS=_px(calls))
    if enabled:
        env["STUB_ENABLED"] = "1"
    if fail:
        env["STUB_SYSTEMCTL_FAIL"] = fail
    proc = subprocess.run([_bash(), _px(script)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    return proc, [c for c in calls.read_text(encoding="utf-8").splitlines()]


def _install_and_reload(calls):
    return calls[0].startswith("install -m 0644 ") and calls[0].endswith(" /etc/systemd/system/") \
        and "homehub-sure.service" in calls[0] and calls[1] == "systemctl daemon-reload"


def test_tc1361_knob_true_installs_enables_and_queues_the_start_sr100(tmp_path):
    proc, calls = _run_block(tmp_path, "COMPOSE_PROFILES=ntfy\nSURE_ENABLED=true\n")
    assert _install_and_reload(calls), calls
    assert calls[2:] == ["systemctl enable homehub-sure.service",
                         "systemctl start --no-block homehub-sure.service"], calls
    assert "start queued" in proc.stdout


def test_tc1361_crlf_and_quoted_knob_still_reads_true_sr100(tmp_path):
    _, calls = _run_block(tmp_path, 'SURE_ENABLED="true"\r\n')
    assert "systemctl start --no-block homehub-sure.service" in calls


def test_tc1361_knob_absent_touches_no_running_sure_sr100(tmp_path):
    """The live hub on 2026-10-22: no SURE_ENABLED line, Sure running from its
    first start. An upgrade of this script must not stop the finance database."""
    proc, calls = _run_block(tmp_path, "COMPOSE_PROFILES=ntfy\n")
    assert _install_and_reload(calls), calls
    assert calls[2:] == ["systemctl is-enabled homehub-sure.service"], calls
    assert "no Sure container was touched" in proc.stdout


def test_tc1361_knob_false_after_the_unit_owned_sure_disables_and_queues_the_stop_sr100(tmp_path):
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True)
    assert calls[2:] == ["systemctl is-enabled homehub-sure.service",
                         "systemctl disable homehub-sure.service",
                         "systemctl stop --no-block homehub-sure.service"], calls
    assert "DISABLED" in proc.stdout


def test_tc1361_a_failed_start_queue_is_a_warning_not_an_abort_sr100(tmp_path):
    proc, _ = _run_block(tmp_path, "SURE_ENABLED=true\n", fail="start")
    assert "could not queue the start" in proc.stdout


def test_tc1361_a_sure_profile_is_warned_about_sr100(tmp_path):
    proc, _ = _run_block(tmp_path, "COMPOSE_PROFILES=ntfy,sure\nSURE_ENABLED=true\n")
    assert "WARN: COMPOSE_PROFILES names 'sure'" in proc.stdout
    proc, _ = _run_block(tmp_path, "COMPOSE_PROFILES=ntfy,surely\nSURE_ENABLED=true\n")
    assert "COMPOSE_PROFILES names" not in proc.stdout


def test_tc1361_no_payload_skips_cleanly_sr100(tmp_path):
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=true\n", payload=False)
    assert calls == [] and "no payload" in proc.stdout


def test_tc1361_no_blocking_sure_start_anywhere_in_firstboot_sr100():
    """A blocking start from inside firstboot waits on firstboot itself (the
    unit is After=homehub-firstboot.service)."""
    text = _live(FIRSTBOOT.read_text(encoding="utf-8"))
    for m in re.finditer(r"systemctl\s+[^\n|;&]*\b(?:start|restart|stop)\b[^\n|;&]*homehub-sure\.service", text):
        assert "--no-block" in m.group(0), m.group(0)
    assert "enable --now homehub-sure" not in text


def test_tc1361_the_template_ships_sure_on_by_knob_not_profile_sr100():
    lines = ENVEX.read_text(encoding="utf-8").splitlines()
    profiles = [l.split("=", 1)[1] for l in lines if l.startswith("COMPOSE_PROFILES=")]
    assert profiles and "sure" not in profiles[-1].split(","), profiles
    assert [l for l in lines if l.startswith("SURE_ENABLED=")] == ["SURE_ENABLED=true"]
