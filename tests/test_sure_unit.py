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


def _run_script(tmp_path, verb, env_text="SURE_ENABLED=true\n", fail=False,
                docker_stub=DOCKER_STUB, extra_env=None):
    stack = tmp_path / "stack"
    stack.mkdir(exist_ok=True)
    _w(stack / ".env", env_text)
    docker = tmp_path / "docker"
    _w(docker, docker_stub)
    docker.chmod(0o755)
    calls = tmp_path / "calls.txt"
    _w(calls, "")
    env = dict(os.environ, STACK_DIR=_px(stack), DOCKER=_px(docker), CALLS=_px(calls))
    env.pop("SERVICE_RESULT", None)
    env.update(extra_env or {})
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
    # systemd never runs ExecStop= after a failed ExecStart=: the clean-up of a
    # half-started Sure is ExecStopPost= (W10-sA review F1).
    assert _unit_values("ExecStopPost") == ["/bin/bash %s stop-after-failure" % script]
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
# A STATEFUL systemctl, modelled on what systemd 255 was measured to do (W10-sA
# review rounds 2 and 3): the unit's state, job and Result live in $STUB_DIR.
#  - is-active prints the state (unless --quiet), exit 0 only for active,
#    reloading and refreshing - nonzero for `activating`;
#  - stop of a unit with only a WAITING start job cancels the job and runs no
#    hook at all (measured: no ExecStart, ExecStop or ExecStopPost);
#  - stop of an activating oneshot SIGTERMs ExecStart and runs ExecStopPost with
#    SERVICE_RESULT=signal, leaving the unit failed/signal;
#  - stop of an active unit runs ExecStop, then ExecStopPost with success;
#  - reset-failed turns failed into inactive, and fails on anything else.
# The hooks are the REAL sure-unit.sh; their compose calls go to a separate log
# so the main call log holds only what firstboot itself ran.
_sget() { cat "$STUB_DIR/$1" 2>/dev/null || true; }
_sset() { printf '%s' "$2" > "$STUB_DIR/$1"; }
_hook() {
    echo "[unit] $1 SERVICE_RESULT=${2:-}" >> "$CALLS"
    CALLS="$STUB_DIR/hook-calls" SERVICE_RESULT="${2:-}" STACK_DIR="$STACK_DIR" \
        bash "$STACK_DIR/sure/sure-unit.sh" "$1" >/dev/null 2>&1
}
systemctl() {
    echo "systemctl $*" >> "$CALLS"
    if [ "$1" = is-enabled ]; then [ -n "${STUB_ENABLED:-}" ]; return; fi
    if [ "$1" = is-active ]; then
        local s; s="$(_sget state)"
        [ "${2:-}" = --quiet ] || echo "$s"
        case "$s" in active|reloading|refreshing) return 0 ;; *) return 3 ;; esac
    fi
    if [ "$1" = show ]; then
        case "$*" in *"-p Job "*) _sget job ;; *"-p Result "*) _sget result ;; esac
        echo; return 0
    fi
    if [ "${STUB_SYSTEMCTL_FAIL:-}" = "$1" ]; then return 1; fi
    case "$1" in
        stop)
            case "$(_sget state)" in
                activating) _hook stop-after-failure signal || true
                            _sset state failed; _sset result signal ;;
                active|reloading|refreshing)
                    if _hook stop "" && _hook stop-after-failure success; then
                        _sset state inactive; _sset result success
                    else
                        _sset state failed; _sset result exit-code
                    fi ;;
                deactivating) _sset state inactive ;;
            esac
            _sset job "" ;;
        reset-failed)
            [ "$(_sget state)" = failed ] || return 1
            _sset state inactive; _sset result success ;;
    esac
    return 0
}
# the bounded blocking stop: the stub runs the command unless told it ran out
timeout() {
    echo "timeout $1" >> "$CALLS"; shift
    [ -z "${STUB_TIMEOUT_EXPIRES:-}" ] || return 124
    "$@"
}
install() { echo "install $*" >> "$CALLS"; return 0; }
'''


def _run_block(tmp_path, env_text, enabled=False, fail=None, payload=True, active=False,
               docker_stub=DOCKER_STUB, extra_env=None, state=None, job=None, result="success"):
    """Run firstboot's real 4-pre-a2b block. The payload is the real unit AND
    the real sure-unit.sh, because the off branch runs that script directly;
    its docker is a stub that logs `compose ...` into the same call log."""
    stack = tmp_path / "stack"
    (stack / "sure").mkdir(parents=True, exist_ok=True)
    if payload:
        shutil.copy(UNIT, stack / "sure" / UNIT.name)
        shutil.copy(SCRIPT, stack / "sure" / SCRIPT.name)
    _w(stack / ".env", env_text)
    calls = tmp_path / "calls.txt"
    _w(calls, "")
    docker = tmp_path / "docker"
    _w(docker, docker_stub)
    docker.chmod(0o755)
    script = tmp_path / "block.sh"
    _w(script, _PREAMBLE + 'STACK_DIR="%s"\n' % _px(stack) + _sure_block() + "\necho BLOCK-DONE\n")
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    _w(stub / "state", state or ("active" if active else "inactive"))
    _w(stub / "job", job or "")
    _w(stub / "result", result)
    env = dict(os.environ, CALLS=_px(calls), DOCKER=_px(docker), STUB_DIR=_px(stub), **(extra_env or {}))
    env.pop("SERVICE_RESULT", None)
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
    # nothing after is-enabled: no disable, no stop, and no docker call at all
    assert calls[2:] == ["systemctl is-enabled homehub-sure.service"], calls
    assert "no Sure container was touched" in proc.stdout


COMPOSE_STOP = "compose --profile sure stop -t 60 sure-web sure-worker sure-redis sure-postgres"
_THROUGH_SYSTEMD = ["timeout 330", "systemctl stop homehub-sure.service"]
_RESULT_CHECK = ["systemctl is-active homehub-sure.service",
                 "systemctl show -p Result --value homehub-sure.service"]


def _state(tmp_path):
    return (tmp_path / "stub" / "state").read_text(encoding="utf-8")


def test_tc1361_knob_false_after_the_unit_owned_sure_disables_stops_it_then_runs_the_script_sr100(tmp_path):
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, active=True)
    assert calls[2:] == ["systemctl is-enabled homehub-sure.service",
                         "systemctl disable homehub-sure.service",
                         "systemctl is-active homehub-sure.service",
                         "systemctl show -p Job --value homehub-sure.service",
                         "timeout 330",
                         "systemctl stop homehub-sure.service",
                         "[unit] stop SERVICE_RESULT=",
                         "[unit] stop-after-failure SERVICE_RESULT=success",
                         COMPOSE_STOP] + _RESULT_CHECK, calls
    assert "DISABLED" in proc.stdout and _state(tmp_path) == "inactive"


def test_tc1361_knob_false_with_a_failed_unit_runs_the_stop_script_directly_sr100(tmp_path):
    """`systemctl stop` of a failed unit runs nothing (W10-sA review F1): the
    off branch must stop the containers itself, through the real script."""
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, active=False)
    assert calls[2:] == ["systemctl is-enabled homehub-sure.service",
                         "systemctl disable homehub-sure.service",
                         "systemctl is-active homehub-sure.service",
                         "systemctl show -p Job --value homehub-sure.service",
                         COMPOSE_STOP], calls
    assert "and its stop script ran directly" in proc.stdout


_OFF_PREFIX = ["systemctl is-enabled homehub-sure.service",
               "systemctl disable homehub-sure.service",
               "systemctl is-active homehub-sure.service",
               "systemctl show -p Job --value homehub-sure.service"]


@pytest.mark.parametrize("state,job,result,hooks,reset", [
    # the round-2 case: a queued start still in ExecStart - SIGTERM, ExecStopPost,
    # failed/signal, cleared once the script has succeeded (round 3 F2)
    ("activating", "4711", "success", ["[unit] stop-after-failure SERVICE_RESULT=signal"], True),
    ("activating", "", "success", ["[unit] stop-after-failure SERVICE_RESULT=signal"], True),
    ("reloading", "", "success", ["[unit] stop SERVICE_RESULT=",
                                  "[unit] stop-after-failure SERVICE_RESULT=success"], False),
    ("deactivating", "4712", "success", [], False),
    # the every-boot case: a start job WAITING behind firstboot (After=) - the
    # cancel runs no hook at all (round 3 F1)
    ("inactive", "4711", "success", [], False),
    # a genuine earlier failure with a queued start: cancelled, never cleared
    ("failed", "4711", "exit-code", [], False),
])
def test_tc1361_knob_false_with_a_start_in_flight_stops_through_systemd_then_the_script_sr100(
        tmp_path, state, job, result, hooks, reset):
    """W10-sA review round 2 F1: `systemctl is-active` is nonzero for
    `activating`, so a direct stop raced the start firstboot had queued. Round 3
    F1: stopping ONLY through systemd left Docker's own restart of the four
    running when the cancelled start was merely waiting. So: a bounded,
    blocking `systemctl stop` first, then the script, never the other way round."""
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True,
                             state=state, job=job, result=result)
    tail = calls[2 + len(_OFF_PREFIX):]
    assert calls[2:2 + len(_OFF_PREFIX)] == _OFF_PREFIX, calls
    assert tail == _THROUGH_SYSTEMD + hooks + [COMPOSE_STOP] + _RESULT_CHECK + (
        ["systemctl reset-failed homehub-sure.service"] if reset else []), calls
    assert "so it was stopped through systemd and then its stop script ran directly" in proc.stdout
    assert ("it was '%s' with job %s" % (state, job) if job else "it was '%s' with no job" % state) \
        in proc.stdout
    assert "systemctl stop --no-block" not in "\n".join(calls)
    assert _state(tmp_path) == ("failed" if state == "failed" else "inactive")


@pytest.mark.parametrize("state,job", [("inactive", ""), ("failed", ""), ("failed", "0"), ("unknown", "")])
def test_tc1361_knob_false_with_an_idle_unit_runs_the_script_and_no_systemctl_stop_sr100(tmp_path, state, job):
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, state=state, job=job,
                             result="signal" if state == "failed" else "success")
    assert calls[2:] == _OFF_PREFIX + [COMPOSE_STOP], calls
    assert "it was '%s' with no job, and its stop script ran directly" % state in proc.stdout
    # an idle failed unit is a failure from before this run: it stays red
    assert _state(tmp_path) == state


# -- TC-1361: the off switch, stateful (W10-sA review round 3 F1, F2) ----------

def test_tc1361_knob_off_with_a_waiting_start_stops_the_containers_docker_restarted_sr100(tmp_path):
    """Round 3 F1, the every-boot sequence. The unit is enabled, WantedBy=
    multi-user.target and After=homehub-firstboot.service, so its start job
    waits while firstboot runs; Docker has meanwhile brought all four back under
    `restart: unless-stopped`. The Owner set SURE_ENABLED=false and rebooted.
    Cancelling the waiting job runs no hook (measured on systemd 255), so only
    the direct script can stop them - and the disabled unit never takes this
    branch again."""
    running = tmp_path / "running.txt"
    _w(running, "sure-postgres\nsure-redis\nsure-web\nsure-worker\n")
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, state="inactive",
                             job="4711", docker_stub=STATEFUL_DOCKER,
                             extra_env={"RUNNING": _px(running)})
    assert not [c for c in calls if c.startswith("[unit]")], calls      # no hook ran
    assert COMPOSE_STOP in calls, calls                                   # Docker stop called
    assert _running(running) == [], (proc.stdout, calls)                  # no container left
    assert "systemctl disable homehub-sure.service" in calls
    assert (tmp_path / "stub" / "job").read_text(encoding="utf-8") == ""  # the start is cancelled


def test_tc1361_an_intentional_cancel_of_a_running_start_leaves_the_unit_not_failed_sr100(tmp_path):
    """Round 3 F2. SIGTERM of the activating oneshot leaves it failed/signal
    even after ExecStopPost cleaned up, and verify-hub.sh reports DEGRADED and
    `unit FAILED: homehub-sure.service` for an off switch that worked."""
    running = tmp_path / "running.txt"
    _w(running, "sure-postgres\nsure-redis\n")       # `up -d` waiting on their health
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, state="activating",
                             job="4711", docker_stub=STATEFUL_DOCKER,
                             extra_env={"RUNNING": _px(running)})
    assert "[unit] stop-after-failure SERVICE_RESULT=signal" in calls
    assert _running(running) == []
    assert calls[-1] == "systemctl reset-failed homehub-sure.service", calls
    assert _state(tmp_path) == "inactive"
    assert "it was reset-failed" in proc.stdout


def test_tc1361_a_cancel_whose_clean_up_failed_stays_failed_sr100(tmp_path):
    running = tmp_path / "running.txt"
    _w(running, "sure-postgres\nsure-redis\n")
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, state="activating",
                             job="4711", docker_stub=STATEFUL_DOCKER,
                             extra_env={"RUNNING": _px(running), "DOCKER_STOP_FAILS": "1"})
    assert "systemctl reset-failed homehub-sure.service" not in calls, calls
    assert _state(tmp_path) == "failed"
    assert _running(running) == ["sure-postgres", "sure-redis"]
    assert "WARN: homehub-sure.service DISABLED, but its stop script failed" in proc.stdout


def test_tc1361_a_stop_that_ran_out_of_time_still_runs_the_script_and_clears_nothing_sr100(tmp_path):
    running = tmp_path / "running.txt"
    _w(running, "sure-postgres\nsure-redis\n")
    proc, calls = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, state="activating",
                             job="4711", docker_stub=STATEFUL_DOCKER,
                             extra_env={"RUNNING": _px(running), "STUB_TIMEOUT_EXPIRES": "1"})
    assert calls[2 + len(_OFF_PREFIX):] == ["timeout 330", COMPOSE_STOP], calls
    assert _running(running) == []
    assert "did not finish in 330 s" in proc.stdout


def test_tc1361_a_failed_direct_stop_is_a_warning_not_an_abort_sr100(tmp_path):
    proc, _ = _run_block(tmp_path, "SURE_ENABLED=false\n", enabled=True, extra_env={"DOCKER_FAIL": "1"})
    assert "WARN: homehub-sure.service DISABLED, but its stop script failed" in proc.stdout


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
    bounded = 0
    for m in re.finditer(r"systemctl\s+[^\n|;&]*\b(start|restart|stop)\b[^\n|;&]*homehub-sure\.service", text):
        if "--no-block" in m.group(0):
            continue
        # The one blocking call allowed is the off branch's STOP, bounded: a stop
        # job is ordered before a start job whatever the After= direction, so it
        # cannot wait on firstboot (measured on systemd 255, round 3 F1).
        assert m.group(1) == "stop" and re.search(r"\btimeout \d+ $", text[:m.start()]), m.group(0)
        bounded += 1
    assert bounded == 1
    assert "enable --now homehub-sure" not in text


def test_tc1361_the_template_ships_sure_on_by_knob_not_profile_sr100():
    lines = ENVEX.read_text(encoding="utf-8").splitlines()
    profiles = [l.split("=", 1)[1] for l in lines if l.startswith("COMPOSE_PROFILES=")]
    assert profiles and "sure" not in profiles[-1].split(","), profiles
    assert [l for l in lines if l.startswith("SURE_ENABLED=")] == ["SURE_ENABLED=true"]


# ── TC-1360: a failed start is cleaned up (W10-sA review F1) ─────────────────
#
# systemd runs ExecStop= only for a unit whose start succeeded. `up -d` can
# start Postgres and Redis and then fail on a health dependency; those two keep
# `restart: unless-stopped`, so without a clean-up they come back on every boot
# even after the knob goes off. ExecStopPost= runs after every run and gets
# $SERVICE_RESULT; measured on systemd 255 (the hub's) with transient units:
# exec-condition (a skipped ExecCondition=), exit-code (a failed ExecStart=),
# timeout (TimeoutStartSec=), success (a clean start, then stop).

@pytest.mark.parametrize("result", ["exit-code", "timeout", "signal", "core-dump"])
def test_tc1360_stop_after_failure_stops_the_four_after_a_failed_run_sr100(tmp_path, result):
    proc, calls = _run_script(tmp_path, "stop-after-failure", extra_env={"SERVICE_RESULT": result})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == ["compose --profile sure stop -t 60 sure-web sure-worker sure-redis sure-postgres"]
    assert "SERVICE_RESULT=%s" % result in proc.stdout


@pytest.mark.parametrize("extra", [{"SERVICE_RESULT": "success"},
                                   {"SERVICE_RESULT": "exec-condition"},
                                   {}])
def test_tc1360_stop_after_failure_touches_nothing_after_success_or_a_skip_sr100(tmp_path, extra):
    """`exec-condition` is the knob-off skip: this unit started nothing, and on a
    box whose Sure predates the unit nothing may be stopped."""
    proc, calls = _run_script(tmp_path, "stop-after-failure", extra_env=extra)
    assert proc.returncode == 0 and calls == [], (proc.stdout, calls)


# Postgres and Redis come up, then compose fails (a health dependency that
# never becomes ready) when DOCKER_UP_FAILS is set; `stop` stops what it names.
STATEFUL_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
case " $* " in
    *" up "*)
        printf 'sure-postgres\nsure-redis\n' > "$RUNNING"
        [ -n "${DOCKER_UP_FAILS:-}" ] && exit 1
        printf 'sure-postgres\nsure-redis\nsure-web\nsure-worker\n' > "$RUNNING" ;;
    *" stop "*)
        [ -n "${DOCKER_STOP_FAILS:-}" ] && exit 1
        for c in "$@"; do grep -vx -- "$c" "$RUNNING" > "$RUNNING.new"; mv "$RUNNING.new" "$RUNNING"; done ;;
esac
exit 0
"""


def _running(path):
    return sorted(l for l in path.read_text(encoding="utf-8").splitlines() if l)


def test_tc1360_a_failed_start_then_the_knob_off_leaves_no_sure_container_running_sr100(tmp_path):
    """The sequence the review expected: Sure breaks, so the Owner switches it off.
    1. ExecStart: compose starts Postgres and Redis and fails.
    2. ExecStopPost with the SERVICE_RESULT systemd 255 gives a failed ExecStart=
       stops them - ExecStop= never runs after a failed start.
    3. Even had that clean-up not happened (a failure from before it), the next
       firstboot with SURE_ENABLED=false stops them through the script, because
       `systemctl stop` of the failed unit would run nothing."""
    running = tmp_path / "running.txt"
    _w(running, "")
    env = {"RUNNING": _px(running), "DOCKER_UP_FAILS": "1"}

    proc, _ = _run_script(tmp_path, "start", docker_stub=STATEFUL_DOCKER, extra_env=env)
    assert proc.returncode == 1
    assert _running(running) == ["sure-postgres", "sure-redis"]   # the defect's starting point

    proc, calls = _run_script(tmp_path, "stop-after-failure", docker_stub=STATEFUL_DOCKER,
                              extra_env=dict(env, SERVICE_RESULT="exit-code"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _running(running) == [], calls

    # the same failure with no ExecStopPost clean-up, then the knob goes off
    (tmp_path / "again").mkdir()
    _run_script(tmp_path / "again", "start", docker_stub=STATEFUL_DOCKER, extra_env=env)
    assert _running(running) == ["sure-postgres", "sure-redis"]
    proc, calls = _run_block(tmp_path / "again", "SURE_ENABLED=false\n", enabled=True, active=False,
                             docker_stub=STATEFUL_DOCKER, extra_env={"RUNNING": _px(running)})
    assert _running(running) == [], (proc.stdout, calls)
    assert "systemctl disable homehub-sure.service" in calls


def test_tc1361_a_never_enabled_unit_stops_nothing_even_with_sure_running_sr100(tmp_path):
    """The legacy exception, with containers really up: no knob, unit never
    enabled - the live hub on 2026-10-22."""
    running = tmp_path / "running.txt"
    _w(running, "sure-postgres\nsure-redis\nsure-web\nsure-worker\n")
    for env_text in ("COMPOSE_PROFILES=ntfy\n", "SURE_ENABLED=false\n"):
        proc, calls = _run_block(tmp_path, env_text, enabled=False,
                                 docker_stub=STATEFUL_DOCKER, extra_env={"RUNNING": _px(running)})
        assert not [c for c in calls if c.startswith("compose")], calls
        assert len(_running(running)) == 4


# ── TC-1361: the three readers of the knob agree (W10-sA review F2) ──────────
#
# firstboot read `^SURE_ENABLED=` and stripped double quotes only, while the
# unit allowed indentation and stripped either quote: on SURE_ENABLED='true' the
# unit said on and firstboot took its off branch, disabling and stopping an
# enabled Sure. Each reader is run for real on the same line.

ENVFILE_LIB = ROOT / "scripts" / "lib" / "envfile.sh"
EXPORT_IMAGES = ROOT / "vmtest" / "export-images.sh"
ENSURE_IMAGES = ROOT / "scripts" / "ensure-local-images.sh"

KNOB_LINES = [
    ("SURE_ENABLED=true\n", True),
    ("SURE_ENABLED='true'\n", True),
    ('SURE_ENABLED="true"\n', True),
    ("  SURE_ENABLED=true\n", True),
    ("\tSURE_ENABLED='true'\n", True),
    ("SURE_ENABLED = true\n", True),
    ("SURE_ENABLED= true\n", True),
    ("SURE_ENABLED='true'\r\n", True),
    ('  SURE_ENABLED="true"\r\n', True),
    ("SURE_ENABLED=true   # the finance ledger\n", True),
    ("SURE_ENABLED=true\n  SURE_ENABLED='false'\n", False),   # the last assignment wins
    ("SURE_ENABLED=false\nSURE_ENABLED='true'\n", True),
    ("SURE_ENABLED='false'\n", False),
    ("SURE_ENABLED=True\n", False),
    ("SURE_ENABLED=\n", False),
    ("# SURE_ENABLED=true\n", False),
    ("XSURE_ENABLED=true\n", False),
    ("COMPOSE_PROFILES=sure\n", False),
]


def _bake_reads(tmp_path, key, env_text):
    """The bake's reading: scripts/lib/envfile.sh env_file_value, then the
    whitespace strip both callers apply (asserted below)."""
    env_file = tmp_path / "bake.env"
    _w(env_file, env_text)
    proc = subprocess.run(
        [_bash(), "-c", '. "$1"; env_file_value "$2" "$3" | tr -d "[:space:]"', "bake",
         _px(ENVFILE_LIB), _px(env_file), key],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_tc1361_the_bake_callers_read_the_knob_through_env_file_value_sr100():
    assert "env_file_value \"$ENV_FILE\" SURE_ENABLED | tr -d '[:space:]'" in \
        EXPORT_IMAGES.read_text(encoding="utf-8")
    ensure = ENSURE_IMAGES.read_text(encoding="utf-8")
    assert 'env_get() { env_file_value "$ENV_FILE" "$1"; }' in ensure
    assert "env_get SURE_ENABLED | tr -d '[:space:]'" in ensure
    assert "env_get COMPOSE_PROFILES | tr -d '[:space:]'" in ensure


@pytest.mark.parametrize("env_text,expected", KNOB_LINES)
def test_tc1361_unit_firstboot_and_bake_read_sure_enabled_alike_sr100(tmp_path, env_text, expected):
    (tmp_path / "unit").mkdir()
    unit_proc, _ = _run_script(tmp_path / "unit", "condition", env_text)
    unit_on = unit_proc.returncode == 0

    # an enabled, active unit: a not-true reading disables and stops it
    (tmp_path / "fb").mkdir()
    _, calls = _run_block(tmp_path / "fb", env_text, enabled=True, active=True)
    firstboot_on = "systemctl enable homehub-sure.service" in calls
    firstboot_off = "systemctl disable homehub-sure.service" in calls
    assert firstboot_on != firstboot_off, calls

    bake_on = _bake_reads(tmp_path, "SURE_ENABLED", env_text) == "true"

    assert (unit_on, firstboot_on, bake_on) == (expected, expected, expected), \
        "unit=%s firstboot=%s bake=%s for %r" % (unit_on, firstboot_on, bake_on, env_text)


@pytest.mark.parametrize("env_text,named", [
    ("COMPOSE_PROFILES=ntfy,sure\n", True),
    ("COMPOSE_PROFILES='ntfy,sure'\n", True),
    ('COMPOSE_PROFILES="ntfy,sure"\r\n', True),
    ("  COMPOSE_PROFILES = ntfy, sure\n", True),
    ("COMPOSE_PROFILES=ntfy,surely\n", False),
    ("COMPOSE_PROFILES=ntfy   # sure is a knob now\n", False),
    ("COMPOSE_PROFILES=sure\nCOMPOSE_PROFILES=ntfy\n", False),
])
def test_tc1361_firstboot_warns_on_a_sure_profile_exactly_when_the_resolver_refuses_it_sr100(
        tmp_path, env_text, named):
    proc, _ = _run_block(tmp_path, env_text + "SURE_ENABLED=true\n")
    firstboot_warns = "WARN: COMPOSE_PROFILES names 'sure'" in proc.stdout
    resolver_refuses = "sure" in _bake_reads(tmp_path, "COMPOSE_PROFILES", env_text).split(",")
    assert (firstboot_warns, resolver_refuses) == (named, named), proc.stdout
