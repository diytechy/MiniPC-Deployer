"""The maintenance hold: automatic patching must wait while the disk is in surgery.

WHY THIS EXISTS. At 02:30 on 2026-11-01 the weekly unattended upgrade installed
one library (libbrotli1), and needrestart then restarted every service that had
the old copy mapped - including the transient units running a GParted
Resize/Move of an 8 TB NTFS disk, 24h54m and 2.73 TB into the operation.
GParted was terminated mid-move. Nothing on the box knew the move mattered.

WHY THIS RUNS THE SCRIPT. The guard's whole value is its exit status, because
systemd's ExecCondition reads nothing else: 0 runs the upgrade, 1-254 skips it,
255 fails the unit. A guard that printed the right reason and exited 0 would
look perfect in review and protect nothing. The stubs are the two commands it
asks (pgrep, systemctl), on PATH ahead of the real ones, so this needs no root,
no systemd and no running GParted.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUTOINSTALL = REPO / "stack" / "autoinstall"
SCRIPT = AUTOINSTALL / "homehub-maintenance-hold.sh"
NEEDRESTART_CONF = AUTOINSTALL / "80-homehub-maintenance-hold.conf"
FIRSTBOOT = AUTOINSTALL / "firstboot.sh"
INSTALLED_PATH = "/usr/local/sbin/homehub-maintenance-hold"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None,
                                reason="POSIX sh is absent (Windows dev box without git-bash)")

GPARTED_UNITS = ("ext4-gparted.service loaded active running /usr/sbin/gparted /dev/sdb\n"
                 "ext4-gparted-xvfb.service loaded active running /usr/bin/Xvfb :77\n")


def _write_lf(path: Path, text: str) -> None:
    # newline="\n" EXPLICITLY: the dev box is Windows, and a CRLF stub makes
    # /bin/sh choke on the carriage return with a message that blames the
    # script under test.
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    _write_lf(path, "#!/bin/sh\n" + body)
    path.chmod(0o755)


def run(tmp_path: Path, *args: str, processes: str = "", units: str = "",
        pgrep_rc: int | None = None, systemctl_rc: int = 0,
        upgraded_days_ago: float | None = None):
    """Run the guard against stub pgrep/systemctl and a private hold directory.

    processes: what `pgrep -a -x` would print ("PID CMDLINE" lines).
    units:     what `systemctl list-units --plain --no-legend` would print.
    pgrep_rc:  force pgrep's exit status (default: 0 if it printed, else 1).
    upgraded_days_ago: age of apt's upgrade-stamp; None = no stamp at all. The
               stamp is always a private path, so a dev box's real
               /var/lib/apt can never change a verdict here.
    """
    stamp = tmp_path / "upgrade-stamp"
    if upgraded_days_ago is not None:
        stamp.touch()
        stamped = time.time() - upgraded_days_ago * 86400
        os.utime(stamp, (stamped, stamped))
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    _write_lf(tmp_path / "processes.txt", processes)
    _write_lf(tmp_path / "units.txt", units)
    # The pattern pgrep was handed is recorded so a test can prove the
    # disk-tool list really reaches it, not just that the stub answered.
    pgrep_exit = (pgrep_rc if pgrep_rc is not None else (0 if processes else 1))
    _stub(stubs, "pgrep", f'for a in "$@"; do last="$a"; done\n'
          f'printf "%s\\n" "$last" > "{(tmp_path / "pgrep-pattern.txt").as_posix()}"\n'
          f'cat "{(tmp_path / "processes.txt").as_posix()}"\n'
          f"exit {pgrep_exit}\n")
    _stub(stubs, "systemctl",
          f'[ {systemctl_rc} -eq 0 ] || {{ echo "Failed to connect to bus" >&2; exit {systemctl_rc}; }}\n'
          f'cat "{(tmp_path / "units.txt").as_posix()}"\n')
    environment = dict(os.environ,
                       PATH=str(stubs) + os.pathsep + os.environ["PATH"],
                       HOMEHUB_MAINTENANCE_HOLD_DIR=str(tmp_path / "holds"),
                       HOMEHUB_MAINTENANCE_HOLD_STAMP=str(stamp))
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True,
                          env=environment, cwd=str(tmp_path))


# ── check: the verdict systemd reads ─────────────────────────────────────────

def test_a_quiet_box_is_clear_and_exits_zero_sr137(tmp_path):
    done = run(tmp_path, "check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "clear" in done.stdout


def test_a_running_gparted_holds_and_names_itself_sr137(tmp_path):
    done = run(tmp_path, "check", processes="3600903 /usr/libexec/gpartedbin /dev/sdb\n")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "gpartedbin" in done.stdout


def test_the_disk_tool_list_reaches_pgrep_as_one_exact_match_pattern_sr137(tmp_path):
    run(tmp_path, "check")
    pattern = (tmp_path / "pgrep-pattern.txt").read_text().strip()
    # mke2fs is normally run AS mkfs.ext4, and the process name is the name it
    # was run as - so the family must be matched, not just the binary.
    for name in ("gpartedbin", "ntfsresize", "ntfsclone", "resize2fs", "e2fsck",
                 "partclone.ntfs", "fsck.ext4", "mkfs.ext4", "mkfs.ntfs", "mkntfs",
                 "pvmove", "dd", "e4defrag"):
        # pgrep -x anchors the pattern at both ends; emulate that here.
        assert re.fullmatch(f"(?:{pattern})", name), f"{name} would not hold the box"
    for name in ("gparted", "rsync", "bash", "e2fsckx", "add", "ddx"):
        assert not re.fullmatch(f"(?:{pattern})", name), f"{name} would hold the box"


def test_an_active_protected_unit_holds_even_with_gparted_idle_sr137(tmp_path):
    # A GParted session whose window is open but idle still holds: the Owner
    # may press Apply at any moment, and Xvfb dying takes the window with it.
    done = run(tmp_path, "check", units=GPARTED_UNITS)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "ext4-gparted.service" in done.stdout
    assert "ext4-gparted-xvfb.service" in done.stdout


def test_an_operator_hold_file_holds_with_its_reason_sr137(tmp_path):
    held = run(tmp_path, "hold", "pribackup", "PriBackup", "same-disk", "copy")
    assert held.returncode == 0, held.stdout + held.stderr
    done = run(tmp_path, "check")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "pribackup" in done.stdout
    assert "PriBackup same-disk copy" in done.stdout


def test_every_reason_is_reported_not_just_the_first_sr137(tmp_path):
    run(tmp_path, "hold", "manual")
    done = run(tmp_path, "check", processes="42 /usr/sbin/ntfsresize /dev/sdb1\n",
               units=GPARTED_UNITS)
    assert done.returncode == 1
    for reason in ("ntfsresize", "ext4-gparted.service", "manual"):
        assert reason in done.stdout


def test_a_probe_that_cannot_answer_fails_the_unit_rather_than_allowing_it_sr137(tmp_path):
    # 255 is the one ExecCondition status that turns the unit RED. A probe error
    # must not read as clear (the upgrade would run blind) nor as an ordinary
    # hold (the box would skip patching silently, week after week).
    done = run(tmp_path, "check", systemctl_rc=1)
    assert done.returncode == 255, done.stdout + done.stderr
    assert "systemctl" in done.stderr


def test_a_pgrep_failure_is_a_probe_error_not_an_empty_answer_sr137(tmp_path):
    done = run(tmp_path, "check", pgrep_rc=3)
    assert done.returncode == 255, done.stdout + done.stderr


def test_a_hold_that_outlives_its_limit_turns_the_unit_red_sr137(tmp_path):
    # A skipped unit is invisible to monitoring; a failed one is reported. An
    # idle GParted window left open for weeks must not stop patching quietly.
    done = run(tmp_path, "check", units=GPARTED_UNITS, upgraded_days_ago=20)
    assert done.returncode == 255, done.stdout + done.stderr
    assert "20 days" in done.stderr
    assert "ext4-gparted.service" in done.stdout   # still says WHY it is held


def test_the_second_held_sunday_already_turns_red_sr137(tmp_path):
    # Weekly runs at the same minute see a stamp a few seconds short of 7 and
    # 14 whole days. The first held Sunday must stay quiet, the second must not:
    # with a 14-day limit counted in whole days it fired only on the third.
    first = run(tmp_path, "check", units=GPARTED_UNITS, upgraded_days_ago=6.999)
    assert first.returncode == 1, first.stdout + first.stderr
    second = run(tmp_path, "check", units=GPARTED_UNITS, upgraded_days_ago=13.999)
    assert second.returncode == 255, second.stdout + second.stderr


def test_a_recent_upgrade_keeps_an_ordinary_hold_sr137(tmp_path):
    done = run(tmp_path, "check", units=GPARTED_UNITS, upgraded_days_ago=3)
    assert done.returncode == 1, done.stdout + done.stderr


def test_an_overdue_box_that_is_clear_simply_upgrades_sr137(tmp_path):
    done = run(tmp_path, "check", upgraded_days_ago=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_mistyped_subcommand_fails_the_unit_rather_than_skipping_it_sr137(tmp_path):
    done = run(tmp_path, "chek")
    assert done.returncode == 255
    assert "usage" in done.stderr.lower()


# ── hold / release: the operator's own switch ────────────────────────────────

def test_release_lifts_the_hold_sr137(tmp_path):
    run(tmp_path, "hold", "pribackup")
    released = run(tmp_path, "release", "pribackup")
    assert released.returncode == 0, released.stdout + released.stderr
    assert run(tmp_path, "check").returncode == 0


def test_releasing_a_hold_that_does_not_exist_is_loud_sr137(tmp_path):
    done = run(tmp_path, "release", "pribakcup")
    assert done.returncode != 0
    assert "pribakcup" in done.stderr


@pytest.mark.parametrize("name", ["../etc", "a/b", "", "-rf", ".hidden"])
def test_a_hold_name_cannot_escape_the_hold_directory_sr137(tmp_path, name):
    done = run(tmp_path, "hold", name)
    assert done.returncode != 0
    assert not (tmp_path / "etc").exists()
    holds = tmp_path / "holds"
    assert not holds.exists() or not any(holds.iterdir())


# ── wiring: both callers reach the guard at the path firstboot installs ──────

def test_firstboot_installs_the_guard_where_both_callers_look_sr137():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    assert re.search(r"install -m ?0755 \S*homehub-maintenance-hold\.sh\S* " + re.escape(INSTALLED_PATH),
                     text), "firstboot does not install the guard at " + INSTALLED_PATH
    assert f"ExecCondition={INSTALLED_PATH} check" in text
    assert "apt-daily-upgrade.service.d" in text
    conf = NEEDRESTART_CONF.read_text(encoding="utf-8")
    assert INSTALLED_PATH in conf
    assert "80-homehub-maintenance-hold.conf" in text


def _eval_needrestart_conf(tmp_path: Path, guard_rc: int) -> str:
    guard = tmp_path / "guard"
    _stub(tmp_path, "guard", f"exit {guard_rc}\n")
    # The same `eval do { ... <> }` needrestart.conf uses to read conf.d.
    program = ("our %nrconf = (override_rc => {}, override_cont => {});"
               "eval do { local(@ARGV, $/) = $ARGV[0]; <> }; die $@ if $@;"
               "printf qq(rc=%s cont=%s restart=%s\\n),"
               " join(q(,), map { qq($_=$nrconf{override_rc}{$_}) } sort keys %{$nrconf{override_rc}}),"
               " join(q(,), map { qq($_=$nrconf{override_cont}{$_}) } sort keys %{$nrconf{override_cont}}),"
               " $nrconf{restart} // q(unset);")
    environment = dict(os.environ, HOMEHUB_MAINTENANCE_HOLD_BIN=str(guard))
    done = subprocess.run(["perl", "-e", program, str(NEEDRESTART_CONF)],
                          capture_output=True, text=True, env=environment)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
def test_needrestart_restarts_nothing_while_held_sr137(tmp_path):
    out = _eval_needrestart_conf(tmp_path, guard_rc=1)
    assert "rc=(?^:.)=0" in out, out
    assert "cont=(?^:.)=0" in out, out
    # The second layer: interactive mode gets a list, not a dialog.
    assert "restart=l" in out, out


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
def test_needrestart_also_restarts_nothing_when_the_guard_cannot_answer_sr137(tmp_path):
    out = _eval_needrestart_conf(tmp_path, guard_rc=255)
    assert "rc=(?^:.)=0" in out, out


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
def test_needrestart_is_untouched_when_the_box_is_clear_sr137(tmp_path):
    out = _eval_needrestart_conf(tmp_path, guard_rc=0)
    assert out.strip() == "rc= cont= restart=unset", out
