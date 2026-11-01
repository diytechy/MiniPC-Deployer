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


def _baseline(tmp_path: Path) -> Path:
    return tmp_path / "state" / "never-upgraded-since"


def run(tmp_path: Path, *args: str, processes: str = "", units: str = "",
        pgrep_rc: int | None = None, systemctl_rc: int = 0,
        upgraded_days_ago: float | None = None,
        baseline_days_ago: float | None = None):
    """Run the guard against stub pgrep/systemctl and a private hold directory.

    processes: what `pgrep -a -x` would print ("PID CMDLINE" lines).
    units:     what `systemctl list-units --plain --no-legend` would print.
    pgrep_rc:  force pgrep's exit status (default: 0 if it printed, else 1).
    upgraded_days_ago: age of apt's upgrade-stamp; None = no stamp at all. The
               stamp is always a private path, so a dev box's real
               /var/lib/apt can never change a verdict here.
    baseline_days_ago: age of the guard's own no-stamp baseline; None = none yet.
    """
    def aged(path: Path, days_ago: float | None) -> None:
        if days_ago is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        stamped = time.time() - days_ago * 86400
        os.utime(path, (stamped, stamped))

    stamp = tmp_path / "upgrade-stamp"
    aged(stamp, upgraded_days_ago)
    aged(_baseline(tmp_path), baseline_days_ago)
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
                       HOMEHUB_MAINTENANCE_HOLD_STAMP=str(stamp),
                       HOMEHUB_MAINTENANCE_HOLD_STATE_DIR=str(tmp_path / "state"))
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


def test_no_stamp_records_a_baseline_on_the_first_held_run_sr137(tmp_path):
    # A freshly imaged box held before its first unattended run has no stamp.
    done = run(tmp_path, "check", units=GPARTED_UNITS)
    assert done.returncode == 1, done.stdout + done.stderr
    assert _baseline(tmp_path).is_file()


def test_no_stamp_still_turns_red_when_the_hold_drags_on_sr137(tmp_path):
    # Without the baseline this box would skip every Sunday forever (Codex
    # review 2026-11-01): no stamp must not mean no limit.
    done = run(tmp_path, "check", units=GPARTED_UNITS, baseline_days_ago=13.999)
    assert done.returncode == 255, done.stdout + done.stderr


def test_a_baseline_that_cannot_be_recorded_fails_the_unit_sr137(tmp_path):
    # No stamp AND no way to record a baseline used to read as an ordinary
    # hold - skipped forever, silently (Codex review round 2). A regular file
    # where the state directory should be makes the creation fail.
    (tmp_path / "state").write_text("not a directory")
    done = run(tmp_path, "check", units=GPARTED_UNITS)
    assert done.returncode == 255, done.stdout + done.stderr
    assert "baseline" in done.stderr
    assert "ext4-gparted.service" in done.stdout   # still says WHY it is held


def test_a_real_stamp_wins_and_retires_the_baseline_sr137(tmp_path):
    done = run(tmp_path, "check", units=GPARTED_UNITS,
               upgraded_days_ago=3, baseline_days_ago=30)
    assert done.returncode == 1, done.stdout + done.stderr
    assert not _baseline(tmp_path).exists()


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


def test_firstboot_starts_the_timer_only_after_the_guard_is_in_place_sr137():
    # Persistent=true fires a missed run the moment the timer is (re)started.
    # On the first boot after a delivery that run must already meet the guard
    # (Codex review 2026-11-01: the restart used to come first).
    text = FIRSTBOOT.read_text(encoding="utf-8")
    restarts = [m.start() for m in re.finditer(r"^systemctl restart apt-daily-upgrade\.timer", text, re.M)]
    assert len(restarts) == 1, restarts
    guard_pieces = (f"ExecCondition={INSTALLED_PATH} check",
                    "/etc/needrestart/conf.d/80-homehub-maintenance-hold.conf")
    for piece in guard_pieces:
        assert text.index(piece) < restarts[0], f"{piece} comes after the timer restart"
    reload_before = text.rfind("\nsystemctl daemon-reload", 0, restarts[0])
    assert reload_before > text.index(guard_pieces[0]), "no daemon-reload between the drop-in and the restart"


def _eval_needrestart_conf(tmp_path: Path, guard_rc: int, cli_mode: str | None = None,
                           env_mode: str | None = None, mode_var: str = "opt_r") -> str:
    """Evaluate the snippet the way needrestart 3.6 does, then resolve the mode.

    cli_mode: what `-r` already parsed into $opt_r before the config is read.
    env_mode: NEEDRESTART_MODE in the environment.
    The two resolution lines after the eval are needrestart 3.6's own, verbatim
    (/usr/sbin/needrestart on the hub, read 2026-11-01): the snippet is judged
    by the EFFECTIVE mode, not by what it wrote into %nrconf.
    mode_var: the name needrestart keeps its mode in; anything but opt_r
              stands for a future needrestart where setting $main::opt_r is a
              no-op, leaving the snippet's other layers to hold.
    """
    guard = tmp_path / "guard"
    _stub(tmp_path, "guard", f"exit {guard_rc}\n")
    preset = f"$opt_r = q({cli_mode});" if cli_mode else ""
    program = ("use strict; use warnings;"
               "our ($opt_r, %nrconf); %nrconf = (override_rc => {}, override_cont => {});"
               + preset +
               # The same `eval do { ... <> }` needrestart.conf uses to read conf.d.
               "eval do { local(@ARGV, $/) = $ARGV[0]; <> }; die $@ if $@;"
               "my $configured = $nrconf{restart} // q(unset);"
               "$opt_r = $ENV{NEEDRESTART_MODE} if(!defined($opt_r) && exists($ENV{NEEDRESTART_MODE}));"
               "$opt_r = $nrconf{restart} unless(defined($opt_r));"
               "printf qq(rc=%s cont=%s restart=%s effective=%s\\n),"
               " join(q(,), map { qq($_=$nrconf{override_rc}{$_}) } sort keys %{$nrconf{override_rc}}),"
               " join(q(,), map { qq($_=$nrconf{override_cont}{$_}) } sort keys %{$nrconf{override_cont}}),"
               " $configured, $opt_r // q(unset);")
    if mode_var != "opt_r":
        # Rename needrestart's variable everywhere EXCEPT inside the snippet,
        # which still writes $main::opt_r (now a variable nobody reads).
        program = program.replace("$opt_r", "$" + mode_var)
    environment = dict(os.environ, HOMEHUB_MAINTENANCE_HOLD_BIN=str(guard))
    environment.pop("NEEDRESTART_MODE", None)
    if env_mode:
        environment["NEEDRESTART_MODE"] = env_mode
    done = subprocess.run(["perl", "-e", program, str(NEEDRESTART_CONF)],
                          capture_output=True, text=True, env=environment)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
@pytest.mark.parametrize("cli_mode,env_mode", [("i", None), (None, "i"), ("a", "i"), (None, None)])
def test_a_held_box_resolves_to_list_mode_however_interactive_was_asked_sr137(tmp_path, cli_mode, env_mode):
    # In interactive mode override_rc only pre-unticks the dialog; `-r i` or
    # NEEDRESTART_MODE=i used to win over $nrconf{restart} (Codex review
    # 2026-11-01). List mode dispatches no restart at all.
    out = _eval_needrestart_conf(tmp_path, guard_rc=1, cli_mode=cli_mode, env_mode=env_mode)
    assert out.strip().endswith("effective=l"), out


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
def test_if_needrestart_renames_its_mode_variable_the_environment_still_cannot_win_sr137(tmp_path):
    # The fallback layer: with $main::opt_r a dead write, only the deleted
    # NEEDRESTART_MODE stops "i" beating $nrconf{restart} = 'l'.
    out = _eval_needrestart_conf(tmp_path, guard_rc=1, env_mode="i", mode_var="opt_mode")
    assert out.strip().endswith("effective=l"), out


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is absent")
def test_a_clear_box_leaves_an_explicit_interactive_request_alone_sr137(tmp_path):
    out = _eval_needrestart_conf(tmp_path, guard_rc=0, cli_mode="i")
    assert out.strip().endswith("effective=i"), out


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
    assert out.strip() == "rc= cont= restart=unset effective=unset", out
