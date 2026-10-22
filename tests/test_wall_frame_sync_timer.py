"""The frame-sync timer carries the Owner's 30-minute cadence, and it fires.

On 2026-09-15 the Owner put a drop-in on the panel,
wall-sync-frame.timer.d/10-cadence.conf, to slow the frame sync from every
minute to every 30 minutes. It reset the trigger list with an empty
`OnUnitInactiveSec=` first, which systemd applies to the WHOLE list
(systemd.timer(5): "If the empty string is assigned to any of these options,
the list of timers is reset (both monotonic timers and OnCalendar= timers)"),
so it also deleted the base unit's OnBootSec=3min. What remained,
OnUnitInactiveSec=30min, is measured from the service's last deactivation, and
a service that has never run has none: the timer sat `elapsed` with
NextElapseUSecMonotonic=infinity and the frame sync never ran from a timer
again (open-items C47; measured on the panel and reproduced on systemd 255 in
WSL, 2026-10-22). The drop-in also made the paired release refuse
(`unit-has-dropin`).

So the shipped timer now carries the cadence itself, with a trigger that arms
from the timer's own start (TC-1420); firstboot retires exactly that drop-in,
by hash, and nothing else (TC-1421); and firstboot restarts the timer when it
finds it elapsed, because `daemon-reload` does not re-arm one (TC-1422).
TC-1423 runs `systemd-analyze verify` where systemd exists.

The fixture tests/fixtures/wall-sync-frame.timer.d-10-cadence.conf is the
panel's drop-in, copied from the panel byte for byte on 2026-10-22.

Verifies: TC-1420, TC-1421, TC-1422, TC-1423 (SR-112, LLR-1255, LLR-1256, LLR-1257)
"""
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
TIMER = WALL / "wall-sync-frame.timer"
SERVICE = WALL / "wall-sync-frame.service"
FIRSTBOOT = WALL / "wall-firstboot.sh"
OWNER_DROPIN = ROOT / "tests" / "fixtures" / "wall-sync-frame.timer.d-10-cadence.conf"
OWNER_DROPIN_SHA256 = "3eb0011a319e0749b55a8674e989f8b3104df402a9bf014995e1029382df8e1e"

# The base unit as it shipped before SR-112 (the panel's copy on 2026-10-22).
OLD_BASE_TIMER = (
    "[Timer]\nOnBootSec=3min\nOnUnitInactiveSec=1min\nAccuracySec=10s\n"
    "Unit=wall-sync-frame.service\n")

TRIGGERS = ("OnActiveSec", "OnBootSec", "OnStartupSec", "OnUnitActiveSec",
            "OnUnitInactiveSec", "OnCalendar")
# Triggers measured from something that exists before the service first runs.
SELF_ARMING = ("OnActiveSec", "OnBootSec", "OnStartupSec", "OnCalendar")


# ── a model of systemd's trigger-list semantics, for TC-1420 ────────────────
def _timer_assignments(text):
    """(key, value) pairs of the [Timer] section, in file order, duplicates kept."""
    section, pairs = None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Timer]" and "=" in line:
            key, value = line.split("=", 1)
            pairs.append((key.strip(), value.strip()))
    return pairs


def effective_triggers(*texts):
    """The trigger list systemd ends up with after the unit and its drop-ins.

    Every On*Sec= and OnCalendar= appends to ONE list; an empty assignment to
    any of them clears that whole list (systemd.timer(5)).
    """
    triggers = []
    for text in texts:
        for key, value in _timer_assignments(text):
            if key not in TRIGGERS:
                continue
            if value == "":
                triggers = []
            else:
                triggers.append((key, value))
    return triggers


def can_ever_fire(triggers):
    """True when at least one trigger arms before the service has ever run."""
    return any(key in SELF_ARMING for key, _ in triggers)


def test_tc1420_the_model_reproduces_the_panel_llr1255():
    # The panel's pair: base + the Owner's drop-in leaves ONLY the inactive
    # trigger, which is what `systemctl show` printed there (TimersMonotonic=
    # { OnUnitInactiveUSec=30min ; next_elapse=0 }). If this ever passes the
    # check below, the model is blind and the shipped-unit test means nothing.
    pair = effective_triggers(OLD_BASE_TIMER, OWNER_DROPIN.read_text(encoding="utf-8"))
    assert pair == [("OnUnitInactiveSec", "30min")]
    assert not can_ever_fire(pair)
    # Without the empty assignment the drop-in would ADD 30min beside 1min, and
    # the earliest wins: the lesson the drop-in's comment records.
    assert effective_triggers(OLD_BASE_TIMER, "[Timer]\nOnUnitInactiveSec=30min\n") == [
        ("OnBootSec", "3min"), ("OnUnitInactiveSec", "1min"), ("OnUnitInactiveSec", "30min")]


def test_tc1420_the_shipped_timer_carries_the_owners_cadence_and_fires_llr1255():
    text = TIMER.read_text(encoding="utf-8")
    triggers = effective_triggers(text)
    assert sorted(triggers) == [("OnActiveSec", "3min"), ("OnUnitInactiveSec", "30min")], triggers
    assert can_ever_fire(triggers)
    pairs = dict(_timer_assignments(text))
    assert pairs.get("Unit") == "wall-sync-frame.service"
    # No reset in the base unit, no activation-relative cadence (a run longer
    # than the period would re-fire on exit), no catch-up machinery.
    assert ("OnUnitInactiveSec", "") not in _timer_assignments(text)
    assert "OnUnitActiveSec" not in pairs and "Persistent" not in pairs
    assert "OnBootSec" not in pairs, "OnBootSec fires at once on every resume (the timer is restarted then)"
    assert re.search(r"^WantedBy=timers\.target$", text, re.M)


def test_tc1420_the_comment_keeps_the_owners_reasons_and_the_empty_assignment_lesson_llr1255():
    text = TIMER.read_text(encoding="utf-8")
    for needle in ("2026-09-15", "92 s", "152 s", "DUTY-CYCLE", "EMPTY-ASSIGNMENT",
                   "WHOLE trigger list", "NextElapseUSecMonotonic=infinity", "C47"):
        assert needle in text, needle


def test_tc1420_the_service_still_runs_the_frame_flow_only_llr1255():
    text = SERVICE.read_text(encoding="utf-8")
    assert re.search(r"^ExecStart=/usr/local/sbin/wall-sync\.sh --only frame$", text, re.M)


# ── running firstboot's own functions, for TC-1421 / TC-1422 ────────────────
def _bash():
    git = Path("C:/Program Files/Git/bin/bash.exe")
    if git.exists():
        return str(git)
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("bash not available")
    return exe


def _px(path):
    text = Path(path).as_posix()
    if len(text) > 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _function(name):
    """The text of one top-level function in wall-firstboot.sh."""
    text = FIRSTBOOT.read_text(encoding="utf-8")
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), text, re.M | re.S)
    assert match, name
    return match.group(0)


PREAMBLE = (
    "set -euo pipefail\n"
    'log() { echo "LOG $*"; }\n'
    'warn() { echo "WARN $*"; }\n'
    'fail_step() { echo "FAIL $*"; }\n'
)


def _run(tmp_path, body, env=None):
    script = tmp_path / "run.sh"
    script.write_bytes((PREAMBLE + body + "\necho RUN-DONE\n").encode("utf-8"))
    proc = subprocess.run([_bash(), _px(script)], capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))
    assert proc.returncode == 0 and "RUN-DONE" in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout


def _call_site_args():
    """The arguments step 5c passes, so the tests run what the panel runs."""
    text = re.sub(r"\\\n\s*", " ", FIRSTBOOT.read_text(encoding="utf-8"))
    match = re.search(r"^retire_obsolete_dropin\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.M)
    assert match, "step 5c no longer calls retire_obsolete_dropin"
    return match.groups()


def test_tc1421_the_call_site_names_the_owners_dropin_by_its_real_hash_llr1256():
    directory, name, digest, keep = _call_site_args()
    assert directory == "/etc/systemd/system/wall-sync-frame.timer.d"
    assert name == "10-cadence.conf"
    assert digest == OWNER_DROPIN_SHA256
    assert hashlib.sha256(OWNER_DROPIN.read_bytes()).hexdigest() == OWNER_DROPIN_SHA256
    assert not keep.startswith("/etc/systemd"), "the retired copy must be out of systemd's sight"


def _retire(tmp_path, dropin_dir):
    _, name, digest, _ = _call_site_args()
    keep = tmp_path / "keep"
    out = _run(tmp_path, _function("retire_obsolete_dropin")
               + 'retire_obsolete_dropin "%s" %s %s "%s"\n' % (_px(dropin_dir), name, digest, _px(keep)))
    return out, keep / name


def test_tc1421_the_owners_dropin_is_retired_and_the_directory_removed_llr1256(tmp_path):
    d = tmp_path / "wall-sync-frame.timer.d"
    d.mkdir()
    shutil.copyfile(OWNER_DROPIN, d / "10-cadence.conf")
    out, kept = _retire(tmp_path, d)
    assert not d.exists(), out
    assert kept.read_bytes() == OWNER_DROPIN.read_bytes()
    assert "LOG retired the obsolete drop-in" in out and "WARN" not in out and "FAIL" not in out


def test_tc1421_absent_is_a_quiet_no_op_llr1256(tmp_path):
    out, kept = _retire(tmp_path, tmp_path / "wall-sync-frame.timer.d")
    assert "LOG no obsolete drop-in" in out and "WARN" not in out and "FAIL" not in out
    assert not kept.exists()


def test_tc1421_a_different_dropin_of_the_same_name_is_left_and_journalled_llr1256(tmp_path):
    d = tmp_path / "wall-sync-frame.timer.d"
    d.mkdir()
    edited = OWNER_DROPIN.read_bytes().replace(b"OnUnitInactiveSec=30min", b"OnUnitInactiveSec=20min")
    (d / "10-cadence.conf").write_bytes(edited)
    out, kept = _retire(tmp_path, d)
    assert (d / "10-cadence.conf").read_bytes() == edited
    assert not kept.exists()
    assert "WARN drop-in" in out and "NOT the known obsolete one" in out and "unit-has-dropin" in out


def test_tc1421_other_files_in_the_directory_are_left_and_named_llr1256(tmp_path):
    d = tmp_path / "wall-sync-frame.timer.d"
    d.mkdir()
    shutil.copyfile(OWNER_DROPIN, d / "10-cadence.conf")
    (d / "20-other.conf").write_bytes(b"[Timer]\nAccuracySec=1s\n")
    (d / ".hidden.conf").write_bytes(b"[Timer]\n")
    out, kept = _retire(tmp_path, d)
    assert kept.read_bytes() == OWNER_DROPIN.read_bytes()
    assert sorted(p.name for p in d.iterdir()) == [".hidden.conf", "20-other.conf"]
    assert "20-other.conf" in out and ".hidden.conf" in out and out.count("WARN left in place") == 2


def test_tc1421_a_symlink_of_the_same_name_is_left_even_to_the_exact_bytes_llr1256(tmp_path):
    d = tmp_path / "wall-sync-frame.timer.d"
    d.mkdir()
    target = tmp_path / "elsewhere.conf"
    shutil.copyfile(OWNER_DROPIN, target)
    try:
        (d / "10-cadence.conf").symlink_to(target)
    except OSError:
        pytest.skip("this filesystem cannot make a symlink")
    out, kept = _retire(tmp_path, d)
    assert (d / "10-cadence.conf").is_symlink() and not kept.exists()
    assert "WARN drop-in" in out and "not a regular file" in out


def test_tc1421_the_retirement_runs_in_step_5c_before_the_reload_llr1256():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    a = text.index("# ── 5c. the unit files")
    b = text.index("# ── 6. D-W4", a)
    block = text[a:b]
    loop_end = block.index("\ndone\n")
    call = block.index("\nretire_obsolete_dropin ")
    reload_ = block.index("\nsystemctl daemon-reload")
    assert loop_end < call < reload_, "the drop-in is retired after the fixed unit is installed and before systemd reloads"


def _arm(tmp_path, active, sub, restart_rc=0, start_rc=0):
    calls = tmp_path / "calls.txt"
    calls.write_bytes(b"")
    stub = (
        'systemctl() {\n'
        '    echo "systemctl $*" >> "$CALLS"\n'
        '    case "$1 $2" in\n'
        '        "show -p")\n'
        '            case "$3" in ActiveState) echo "$ACTIVE" ;; SubState) echo "$SUB" ;; esac ;;\n'
        '        "restart "*) return "$RESTART_RC" ;;\n'
        '        "start "*) return "$START_RC" ;;\n'
        '    esac\n'
        '    return 0\n'
        '}\n')
    out = _run(tmp_path, stub + _function("arm_elapsed_timer") + "arm_elapsed_timer wall-sync-frame.timer\n",
               env={"CALLS": _px(calls), "ACTIVE": active, "SUB": sub,
                    "RESTART_RC": str(restart_rc), "START_RC": str(start_rc)})
    acting = [c for c in calls.read_text(encoding="utf-8").splitlines() if not c.startswith("systemctl show")]
    return out, acting


def test_tc1422_an_elapsed_timer_is_restarted_llr1257(tmp_path):
    out, acting = _arm(tmp_path, "active", "elapsed")
    assert acting == ["systemctl restart wall-sync-frame.timer"]
    assert "LOG wall-sync-frame.timer was elapsed" in out and "FAIL" not in out


def test_tc1422_a_waiting_timer_is_left_alone_llr1257(tmp_path):
    # A boot, or any re-run of firstboot, must not push a pending trigger back.
    for sub in ("waiting", "running"):
        out, acting = _arm(tmp_path, "active", sub)
        assert acting == [], (sub, acting)
        assert "LOG wall-sync-frame.timer is armed (%s)" % sub in out


def test_tc1422_an_inactive_timer_is_started_llr1257(tmp_path):
    out, acting = _arm(tmp_path, "inactive", "dead")
    assert acting == ["systemctl start wall-sync-frame.timer"]
    assert "LOG wall-sync-frame.timer was inactive; started" in out


def test_tc1422_a_failed_restart_is_a_failed_step_llr1257(tmp_path):
    out, _ = _arm(tmp_path, "active", "elapsed", restart_rc=1)
    assert "FAIL wall-sync-frame.timer is elapsed" in out
    out, _ = _arm(tmp_path, "failed", "failed", start_rc=1)
    assert "FAIL wall-sync-frame.timer is failed and could not be started" in out


def test_tc1422_step_8_arms_the_timer_after_enabling_it_llr1257():
    text = re.sub(r"\\\n\s*", " ", FIRSTBOOT.read_text(encoding="utf-8"))
    enable = text.index('enable_unit "OI-18: wall-sync-frame.timer enabled')
    arm = text.index("arm_elapsed_timer wall-sync-frame.timer", enable)
    assert arm < text.index("\nelse\n", enable), "armed inside the same `if the timer is installed` block"


# ── systemd's own parser, where there is one, for TC-1423 ───────────────────
def test_tc1423_systemd_analyze_verify_accepts_the_pair_llr1255(tmp_path):
    analyze = shutil.which("systemd-analyze")
    if not analyze or os.name == "nt":
        pytest.skip("systemd-analyze not available (run in WSL or on the panel)")
    for unit in (TIMER, SERVICE):
        target = tmp_path / unit.name
        shutil.copyfile(unit, target)
        target.chmod(0o644)
    proc = subprocess.run([analyze, "verify", str(tmp_path / TIMER.name)],
                          capture_output=True, text=True)
    ours = [line for line in (proc.stdout + proc.stderr).splitlines() if "wall-sync-frame" in line]
    assert proc.returncode == 0 and ours == [], proc.stdout + proc.stderr
