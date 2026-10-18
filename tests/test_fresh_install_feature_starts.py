"""A FRESH hub install must END UP with AI_CLI / WEIGHT running, unattended.

Two gaps a review of the reimage path found (LLR-1138, LLR-1139):

  1. setup-ai-cli.sh only ENABLES homehub-ai-cli.service and nothing started it,
     so the API was down on the install boot until a reboot or a hand start.
     firstboot now queues a NON-BLOCKING start, on the success branch only.
  2. setup-weight.sh refused (exit 2) when the tracker's per-user definitions
     directory did not exist yet - and on a fresh install it appears only after
     the tracker has run - so neither unit was installed and nothing ever came
     back to it. It now exits 4 ("not yet") after installing what it can, and
     homehub-weight-setup.{service,timer} re-runs it every 15 minutes until it
     exits 0, then disables itself.

The behaviour is EXECUTED, not grepped: the real setup-weight.sh runs against a
throwaway tree with `install`, `rm`, `systemctl`, `useradd` and `id` replaced by
PATH stubs, and the real firstboot blocks are cut out of firstboot.sh and run in
bash with functions standing in for systemctl/install. The static tests only
cover what cannot be run off the box (unit-file shape, which branch a line sits
in).

Verifies: TC-1259 (LLR-1138), TC-1260 (LLR-1139), TC-1261 (LLR-1139)
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
FIRSTBOOT = STACK / "autoinstall" / "firstboot.sh"
SETUP = STACK / "weight" / "setup-weight.sh"
RETRY_SVC = STACK / "weight" / "homehub-weight-setup.service"
RETRY_TIMER = STACK / "weight" / "homehub-weight-setup.timer"

_HAVE_BASH = bool(os.environ.get("SHELL")) or Path(
    "C:/Program Files/Git/bin/bash.exe").exists() or bool(shutil.which("bash"))
needs_bash = pytest.mark.skipif(not _HAVE_BASH, reason="needs bash")

HEALTH = (
    "---\ncategory: Health\nitems:\n  - id: weigh-in\n    title: Step\n"
    "    target: 170\n    unit: lb\n---\n\nnotes\n"
)
OTHER = "---\ncategory: Chores\n---\n\nnotes\n"


def _w(path, text):
    """Write LF-only text (Path.write_text has no newline= before Python 3.10)."""
    Path(path).write_bytes(text.encode("utf-8"))


def _bash():
    git = Path("C:/Program Files/Git/bin/bash.exe")
    return str(git) if git.exists() else "bash"


# ───────────────────────── firstboot block extraction ─────────────────────────

def _block(start_marker, end_marker):
    text = FIRSTBOOT.read_text(encoding="utf-8")
    a = text.index(start_marker)
    b = text.index(end_marker, a)
    return text[a:b]


def _ai_block():
    return _block('AI_CLI_ENABLED="$(ai_unq "$(_env_val AI_CLI_ENABLED)")"',
                  "# ── mesh VPN subnet router")


def _weight_block():
    return _block('WEIGHT_ENABLED="$(_env_val WEIGHT_ENABLED', "# ── 6f-2. the sleep feeder")


# Functions stand in for the commands the blocks call; `set -euo pipefail` is
# on, exactly as in firstboot, so an unguarded non-zero would abort the run.
_PREAMBLE = r'''
set -euo pipefail
log() { echo "[firstboot] $*"; }
ai_unq() { local v="${1%$'\r'}"; v="${v%\"}"; v="${v#\"}"; printf '%s' "$v"; }
_env_val() { local v="E_$1"; printf '%s' "${!v:-}"; }
systemctl() { echo "systemctl $*" >> "$CALLS"; if [ "${STUB_SYSTEMCTL_FAIL:-}" = "$1" ]; then return 1; fi; return 0; }
install() { echo "install $*" >> "$CALLS"; return 0; }
'''


def _run_block(tmp_path, block, env, stubs):
    stack = tmp_path / "stack"
    for rel, body in stubs.items():
        p = stack / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _w(p, body)
    (stack / "weight").mkdir(parents=True, exist_ok=True)
    for f in ("homehub-weight-setup.service", "homehub-weight-setup.timer"):
        shutil.copy(STACK / "weight" / f, stack / "weight" / f)
    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    script = tmp_path / "block.sh"
    nl = chr(10)
    _w(script, _PREAMBLE + 'STACK_DIR="%s"' % stack.as_posix() + nl + block + nl
       + "echo BLOCK-DONE" + nl)
    e = dict(os.environ, CALLS=calls.as_posix(), **env)
    proc = subprocess.run([_bash(), str(script)], env=e, capture_output=True, text=True)
    return proc, calls.read_text(encoding="utf-8").splitlines()


# ───────────────────────────── Fix 1: the AI CLI start ─────────────────────────

def _ai_stub(rc):
    return {"ai-cli/setup-ai-cli.sh": "#!/usr/bin/env bash\necho setup-ran\nexit %d\n" % rc}


START = "systemctl start --no-block homehub-ai-cli.service"


@needs_bash
def test_tc1259_ai_cli_start_is_queued_nonblocking_on_success(tmp_path):
    proc, calls = _run_block(tmp_path, _ai_block(), {"E_AI_CLI_ENABLED": "true"}, _ai_stub(0))
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    assert calls == [START], calls


@needs_bash
@pytest.mark.parametrize("rc", [1, 2])
def test_tc1259_ai_cli_is_not_started_when_setup_refuses_or_fails(tmp_path, rc):
    proc, calls = _run_block(tmp_path, _ai_block(), {"E_AI_CLI_ENABLED": "true"}, _ai_stub(rc))
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    assert "WARNING: setup-ai-cli.sh refused or failed" in proc.stdout
    assert calls == [], "a refused/failed setup must not start the service: %r" % calls


@needs_bash
def test_tc1259_ai_cli_knob_off_starts_nothing(tmp_path):
    proc, calls = _run_block(tmp_path, _ai_block(), {"E_AI_CLI_ENABLED": "false"}, _ai_stub(0))
    assert proc.returncode == 0 and calls == []


@needs_bash
def test_tc1259_a_failed_start_queue_is_a_warning_not_an_abort(tmp_path):
    proc, calls = _run_block(tmp_path, _ai_block(),
                             {"E_AI_CLI_ENABLED": "true", "STUB_SYSTEMCTL_FAIL": "start"},
                             _ai_stub(0))
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    assert "could not queue the start" in proc.stdout


def test_tc1259_no_blocking_ai_cli_start_exists_anywhere_in_firstboot():
    """A blocking start from inside firstboot waits on firstboot itself (the
    unit is After=homehub-firstboot.service)."""
    text = FIRSTBOOT.read_text(encoding="utf-8")
    for m in re.finditer(r"systemctl\s+(?:--now\s+)?(?:start|restart|enable\s+--now)\s+([^\n|;&]*homehub-ai-cli\.service)", text):
        assert "--no-block" in m.group(0), m.group(0)
    assert "enable --now homehub-ai-cli.service" not in text


# ───────────────────── Fix 2: the weight setup retry, executed ────────────────

STUB_INSTALL = r'''#!/usr/bin/env bash
map() { case "$1" in /var/lib/*) printf '%s' "$STUB_ROOT$1" ;; *) printf '%s' "$1" ;; esac; }
d=0; args=()
while [ $# -gt 0 ]; do
  case "$1" in -d) d=1 ;; -m|-o|-g) shift ;; *) args+=("$1") ;; esac
  shift
done
if [ "$d" = 0 ] && [ -n "${GATE_INSTALL:-}" ]; then
  touch "$GATE_INSTALL.reached"; while [ -e "$GATE_INSTALL" ]; do sleep 0.1; done
fi
if [ "$d" = 1 ]; then for a in "${args[@]}"; do mkdir -p "$(map "$a")"; done
else dst="$(map "${args[1]}")"; mkdir -p "$(dirname "$dst")"; cp "${args[0]}" "$dst"; fi
'''
STUB_RM = r'''#!/usr/bin/env bash
shopt -s nullglob
out=()
for a in "$@"; do
  case "$a" in
    /var/lib/*) m="$STUB_ROOT$a"; out+=($m) ;;
    -*) out+=("$a") ;;
    *) out+=("$a") ;;
  esac
done
exec "$REAL_RM" "${out[@]}"
'''
STUB_SYSTEMCTL = r'''#!/usr/bin/env bash
if [ "$1 $2 $3" = "disable --now homehub-weight-setup.timer" ] && [ -n "${GATE_DISARM:-}" ]; then
  touch "$GATE_DISARM.reached"; while [ -e "$GATE_DISARM" ]; do sleep 0.1; done
fi
echo "systemctl $*" >> "$CALLS"
exit 0
'''


class Box:
    """A throwaway hub: PATH stubs, a tracker data tree, the real script."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.units = tmp_path / "units"
        self.defs = tmp_path / "tracker" / "sub" / "definitions"
        self.calls = tmp_path / "calls.txt"
        self.calls.write_text("", encoding="utf-8")
        for name, body in (("install", STUB_INSTALL), ("rm", STUB_RM),
                           ("systemctl", STUB_SYSTEMCTL),
                           ("useradd", "#!/usr/bin/env bash\nexit 0\n"),
                           ("id", "#!/usr/bin/env bash\nexit 0\n")):
            p = self.bin / name
            _w(p, body)
            p.chmod(0o755)
        self.real_rm = shutil.which("rm") or "/bin/rm"

    def env(self, **over):
        e = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            REAL_RM=Path(self.real_rm).as_posix(),
            STUB_ROOT=self.root.as_posix(), CALLS=self.calls.as_posix(),
            WEIGHT_LOCK_DIR=(self.tmp / "lock").as_posix(),
            WEIGHT_WAIT_STATE_FILE=(self.tmp / "wait").as_posix(),
            WEIGHT_ENV_FILE="/nonexistent", WEIGHT_ENABLED="true",
            WEIGHT_USER="sub-not-a-real-google-id",
            WEIGHT_FEED_URL="http://127.0.0.1:8099/api/v1/gauges",
            WEIGHT_DEFINITIONS_DIR=self.defs.as_posix(),
            WEIGHT_UNIT_DIR=self.units.as_posix(),
        )
        e.update(over)
        return e

    def run(self, *args, **over):
        # Git-for-Windows bash puts /usr/bin first on startup, which would hide the
        # PATH stubs behind the real install/id/rm; re-prefix PATH inside bash.
        prefix = 'PATH="$(cygpath -u "$STUB_BIN" 2>/dev/null || echo "$STUB_BIN"):$PATH"; exec bash "$0" "$@"'
        return subprocess.run([_bash(), "-c", prefix, str(SETUP), *args],
                              env=self.env(STUB_BIN=str(self.bin), **over),
                              capture_output=True, text=True)

    def systemctl(self):
        return self.calls.read_text(encoding="utf-8").splitlines()

    def put(self, name, body):
        self.defs.mkdir(parents=True, exist_ok=True)
        _w(self.defs / name, body)

    def binds(self):
        conf = self.units / "homehub-weight.service.d" / "10-account.conf"
        return [l for l in conf.read_text(encoding="utf-8").splitlines()
                if l.startswith("BindReadOnlyPaths=")]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


OFF = "disable --now homehub-weight-setup.timer"


@needs_bash
def test_tc1260_data_absent_exits_not_yet_and_the_retry_stays_armed(box):
    assert not box.defs.exists()
    direct = box.run()
    assert direct.returncode == 4, direct.stdout + direct.stderr
    assert "NOT YET" in direct.stdout
    # Everything that CAN be installed is, so the human sign-in steps can proceed.
    assert (box.units / "homehub-weight.service").exists()
    assert (box.units / "homehub-weight.timer").exists()
    assert box.binds() == []
    retry = box.run("--retry")
    assert retry.returncode == 4, retry.stdout + retry.stderr
    assert not any(OFF in c for c in box.systemctl()), box.systemctl()
    # Quiet: ONE line per attempt, not the attempt's twenty.
    assert len(retry.stdout.strip().splitlines()) == 1, retry.stdout


@needs_bash
def test_tc1260_data_present_completes_and_the_retry_disables_itself(box):
    box.put("tracker.md", HEALTH)
    retry = box.run("--retry")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert any(c.endswith(OFF) for c in box.systemctl()), box.systemctl()
    assert any("enable --now homehub-weight.timer" in c for c in box.systemctl())
    (bind,) = box.binds()
    assert bind.startswith("BindReadOnlyPaths=-") and "tracker.md" in bind
    assert len(retry.stdout.strip().splitlines()) == 1, retry.stdout


@needs_bash
def test_tc1260_health_file_arrives_later_the_bind_is_added_by_the_retry(box):
    box.put("chores.md", OTHER)               # the directory exists, Health has not synced
    first = box.run("--retry")
    assert first.returncode == 4, first.stdout + first.stderr
    assert box.binds() == []                  # the bug: no bind, and nothing to repair it
    assert not any(OFF in c for c in box.systemctl())
    box.put("tracker.md", HEALTH)             # the sync lands
    second = box.run("--retry")
    assert second.returncode == 0, second.stdout + second.stderr
    (bind,) = box.binds()
    assert "tracker.md" in bind
    assert any(c.endswith(OFF) for c in box.systemctl())


@needs_bash
def test_tc1260_knob_off_installs_nothing_and_turns_the_retry_off(box):
    box.put("tracker.md", HEALTH)
    for args in ((), ("--retry",)):
        p = box.run(*args, WEIGHT_ENABLED="false")
        assert p.returncode == 0, p.stdout + p.stderr
    assert not box.units.exists()
    calls = box.systemctl()
    assert any(c.endswith(OFF) for c in calls)            # --retry switched it off
    assert not any(" enable" in c for c in calls)


@needs_bash
def test_tc1260_a_config_error_is_not_a_wait(box):
    """exit 2, the retry disables itself and the unit FAILS visibly - it must
    never be mistaken for 'data not there yet'."""
    box.put("tracker.md", HEALTH)
    p = box.run("--retry", WEIGHT_USER="")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "REFUSED" in p.stdout                       # the diagnosis is printed
    assert any(c.endswith(OFF) for c in box.systemctl())
    # A path that EXISTS but is not a directory is a broken config, not absence.
    box.defs.parent.mkdir(parents=True, exist_ok=True)
    f = box.defs.parent / "afile"
    f.write_text("x", encoding="utf-8")
    p2 = box.run(WEIGHT_DEFINITIONS_DIR=f.as_posix())
    assert p2.returncode == 2, p2.stdout + p2.stderr


@needs_bash
def test_tc1260_ambiguous_category_stays_a_refusal_not_a_retry(box):
    box.put("a.md", HEALTH)
    box.put("b.md", HEALTH)
    p = box.run("--retry")
    assert p.returncode == 2, p.stdout + p.stderr
    assert any(c.endswith(OFF) for c in box.systemctl())


def _weight_stub(rc):
    return {"weight/setup-weight.sh": "#!/usr/bin/env bash\necho weight-ran\nexit %d\n" % rc}


ENABLE = "enable --now homehub-weight-setup.timer"
DISABLE = "disable --now homehub-weight-setup.timer"


def _armed(box):
    """The retry timer's state is its LAST enable/disable call; None = untouched."""
    state = None
    for c in box.systemctl():
        if c.endswith(ENABLE):
            state = True
        elif c.endswith(DISABLE):
            state = False
    return state


@needs_bash
def test_tc1260_overlapping_runs_never_leave_data_present_no_bind_nothing_retrying(box):
    """THE RACE. firstboot's setup resolves 'no Health file' and stalls; Health
    arrives; the retry starts and (without the lock) finishes a valid bind and
    disarms, while firstboot -> overwrote the bind with its stale empty answer
    -> and the retry's disarm lands last. End state asserted: the bind is there
    OR the timer is armed - never neither. With the shared lock the retry
    simply waits for firstboot, then does its own full run."""
    import time
    box.put("chores.md", OTHER)                       # dir exists, Health has not synced
    gate_a = box.tmp / "gate-a"
    gate_b = box.tmp / "gate-b"
    gate_a.write_text("x")
    gate_b.write_text("x")
    prefix = ('PATH="$(cygpath -u "$STUB_BIN" 2>/dev/null || echo "$STUB_BIN"):$PATH"; '
              'exec bash "$0" "$@"')

    def start(args, **over):
        return subprocess.Popen(
            [_bash(), "-c", prefix, str(SETUP), *args],
            env=box.env(STUB_BIN=str(box.bin), **over),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_for(path, secs):
        end = time.time() + secs
        while time.time() < end and not path.exists():
            time.sleep(0.05)
        return path.exists()

    a = start([], GATE_INSTALL=gate_a.as_posix())     # "firstboot"
    assert wait_for(Path(gate_a.as_posix() + ".reached"), 30), "run A never reached its install"
    box.put("tracker.md", HEALTH)                     # the sync lands while A is mid-run
    b = start(["--retry"], GATE_DISARM=gate_b.as_posix())   # the timer fires
    wait_for(Path(gate_b.as_posix() + ".reached"), 3)  # unlocked: B gets here; locked: it cannot
    gate_a.unlink()                                   # A resumes: emits its stale empty drop-in
    out_a = a.communicate(timeout=60)[0]
    gate_b.unlink()                                   # B's disarm lands last (if it ran)
    out_b = b.communicate(timeout=60)[0]
    assert a.returncode == 4, out_a
    assert b.returncode == 0, out_b
    has_bind = len(box.binds()) == 1
    armed = _armed(box)
    assert has_bind or armed, (
        "data present, no bind and nothing retrying: bind=%r armed=%r\n%s\n%s"
        % (box.binds(), armed, out_a, out_b))
    # With the lock the retry ran after A, saw Health, and finished the job.
    assert has_bind and armed is False


@needs_bash
def test_tc1260_check_only_agrees_with_setup_not_yet(box):
    # absent directory
    p = box.run("--check-only")
    assert p.returncode == 4 and "NOT YET" in p.stdout, p.stdout
    # directory present, category file absent
    box.put("chores.md", OTHER)
    p = box.run("--check-only")
    assert p.returncode == 4 and "NOT YET" in p.stdout, p.stdout
    assert box.run().returncode == 4                  # and setup says the same
    box.put("tracker.md", HEALTH)
    assert box.run("--check-only").returncode == 0
    assert box.run().returncode == 0


@needs_bash
def test_tc1260_armed_then_refused_disarms_the_retry(box):
    box.put("chores.md", OTHER)
    assert box.run().returncode == 4
    assert _armed(box) is True
    p = box.run(WEIGHT_USER="")                       # a config error appears
    assert p.returncode == 2, p.stdout
    assert _armed(box) is False


@needs_bash
def test_tc1260_persistent_wait_warns_once_a_day_naming_the_path(box):
    box.defs.parent.mkdir(parents=True, exist_ok=True)   # tracker data root, but no definitions/
    over = dict(WEIGHT_WAIT_WARN_AFTER="3")
    outs = [box.run("--retry", **over) for _ in range(3)]
    assert all(o.returncode == 4 for o in outs)
    assert "still waiting" not in outs[0].stdout + outs[1].stdout
    assert "still waiting" in outs[2].stdout
    assert str(box.defs.as_posix()) in outs[2].stdout     # names the configured path
    again = box.run("--retry", **over)                    # same day: quiet, no cutoff
    assert again.returncode == 4 and "still waiting" not in again.stdout
    done = box.run("--retry", **over)
    box.put("tracker.md", HEALTH)
    assert box.run("--retry", **over).returncode == 0
    assert not (box.tmp / "wait").exists()                # counter reset on completion
    assert done.returncode == 4


@needs_bash
def test_tc1260_the_lock_is_released_and_a_dead_holders_lock_is_broken(box):
    box.put("tracker.md", HEALTH)
    assert box.run().returncode == 0
    assert not (box.tmp / "lock").exists()
    (box.tmp / "lock").mkdir()
    (box.tmp / "lock" / "pid").write_text("999999")       # no such process
    assert box.run().returncode == 0


# ───────────────────── Fix 2: firstboot wiring, executed ──────────────────────

def _weight_stub(rc):
    return {"weight/setup-weight.sh": "#!/usr/bin/env bash\necho weight-ran\nexit %d\n" % rc}


@needs_bash
@pytest.mark.parametrize("rc,word", [(4, "INCOMPLETE"), (0, "weight feeder installed;"),
                                     (2, "refused or failed")])
def test_tc1261_firstboot_reports_but_never_decides_the_retry_timer(tmp_path, rc, word):
    """setup-weight.sh settles the timer under its lock; firstboot touching it
    would reopen the race."""
    proc, calls = _run_block(tmp_path, _weight_block(), {"E_WEIGHT_ENABLED": "true"},
                             _weight_stub(rc))
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    assert word in proc.stdout
    assert calls == [], calls


@needs_bash
def test_tc1261_firstboot_knob_off_disables_and_installs_nothing(tmp_path):
    proc, calls = _run_block(tmp_path, _weight_block(), {"E_WEIGHT_ENABLED": "false"},
                             _weight_stub(4))
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout
    assert calls == ["systemctl " + DISABLE], calls


# ───────────────────────────── static shape of the units ──────────────────────

def test_tc1261_retry_units_shape():
    svc = RETRY_SVC.read_text(encoding="utf-8")
    tmr = RETRY_TIMER.read_text(encoding="utf-8")
    lines = [l.strip() for l in svc.splitlines() if not l.strip().startswith("#")]
    assert "ExecStart=/bin/bash /opt/homehub/stack/weight/setup-weight.sh --retry" in lines
    assert "SuccessExitStatus=4" in lines and "Type=oneshot" in lines
    assert "After=homehub-firstboot.service" in lines
    assert "ConditionPathExists=/opt/homehub/stack/.env" in lines
    tl = [l.strip() for l in tmr.splitlines() if not l.strip().startswith("#")]
    assert "OnBootSec=5min" in tl and "OnUnitInactiveSec=15min" in tl
    assert "WantedBy=timers.target" in tl


def test_tc1261_only_setup_arms_the_retry_timer():
    """The arm/disarm decision lives in setup-weight.sh (under the lock); it must
    not reappear in firstboot, where it would race the retry service."""
    fb = FIRSTBOOT.read_text(encoding="utf-8")
    block = fb[fb.index('WEIGHT_ENABLED="$(_env_val WEIGHT_ENABLED'):fb.index("# ── 6f-2.")]
    assert "enable --now homehub-weight-setup.timer" not in block
    on_part, _, off_part = block.partition("\nelse\n")
    assert "disable --now homehub-weight-setup.timer" not in on_part
    assert "disable --now homehub-weight-setup.timer" in off_part
    setup = SETUP.read_text(encoding="utf-8")
    assert 'systemctl enable --now "$RETRY_TIMER"' in setup and "trap on_exit EXIT" in setup


def test_tc1261_every_touched_file_is_lf_only():
    for p in (SETUP, FIRSTBOOT, RETRY_SVC, RETRY_TIMER, Path(__file__)):
        assert b"\r" not in p.read_bytes(), p.name
        assert not p.read_bytes().startswith(b"\xef\xbb\xbf"), p.name
