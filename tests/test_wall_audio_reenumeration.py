"""The audio units follow the USB adapter through a re-enumeration.

Owner item 25 (2026-09-13): the hub was moved to another port, the adapter came
back with the same card id, and the three long-running audio consumers kept
handles on the dead device instance. These tests pin the three mechanisms that
were added: the port-independent udev alias, the BindsTo= on it, and the bounded
alsaloop recover loop.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
RULES = WALL / "90-wall-audio-adapter.rules"
GUARD = WALL / "wall-alsaloop-guard.py"
DIRECT_UNITS = {"wall-line-in.service", "wall-kiosk-loop.service",
                "wall-spdif-in.service", "wall-amp-trigger.service"}
# The escaped form of the ENV{SYSTEMD_ALIAS} path, which is what systemd turns
# it into and what every unit must name.
DEVICE_UNIT = "dev-wall_audio_adapter.device"


def load_guard():
    spec = importlib.util.spec_from_file_location("wall_alsaloop_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(name):
    return (WALL / name).read_text(encoding="utf-8")


# --- the udev rule ---------------------------------------------------------

def test_alias_is_port_independent_and_not_the_kernel_device_name():
    text = RULES.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    assert 'ENV{SYSTEMD_ALIAS}="/dev/wall_audio_adapter"' in body
    assert 'TAG+="systemd"' in body
    # The measured adapter, and only it: the second C-Media on the bus is
    # 0d8c:0014 and is a different device with a different job.
    assert 'ATTRS{idVendor}=="0d8c"' in body
    assert 'ATTRS{idProduct}=="0102"' in body
    assert 'SUBSYSTEM=="sound"' in body and 'KERNEL=="card*"' in body
    # A dash in the alias path would be escaped to \x2d and every unit would
    # have to spell it that way; underscores keep the name literal.
    assert "\\x2d" not in body


def test_every_bound_unit_is_started_again_when_the_device_returns():
    """Derive recovery coverage so a new BindsTo= leg cannot go stale."""
    body = "\n".join(line for line in RULES.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    spec = importlib.util.spec_from_file_location("wall_audio_state",
                                                   WALL / "wall_audio_state.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    directly_started = set(re.findall(r'ENV\{SYSTEMD_WANTS\}\+="([^"]+\.service)"',
                                      body))
    bound = {unit.name for unit in WALL.glob("*.service")
             if "BindsTo=%s" % DEVICE_UNIT in read(unit.name)}
    missing = bound - directly_started - set(policy.ALL_LEGS)
    assert not missing, "adapter-bound unit has no re-add recovery owner: %s" % \
        ", ".join(sorted(missing))
    assert DIRECT_UNITS <= directly_started
    assert "wall-audio-resume.service" in directly_started


def test_the_tag_is_not_restricted_to_add_events():
    """CURRENT_TAGS vs TAGS: an add-only tag is stale by the next event.

    Measured on the panel: card1 carried TAGS=:systemd: but CURRENT_TAGS
    without it, so systemd was not tracking the device at all.
    """
    body = "\n".join(line for line in RULES.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert 'ACTION=="add"' not in body
    assert 'ACTION=="remove", GOTO=' in body


# --- the units -------------------------------------------------------------

@pytest.mark.parametrize("unit", sorted(DIRECT_UNITS | {
    "wall-bus-speaker.service", "wall-speaker-out.service", "wall-mic-rear.service"}))
def test_units_are_bound_to_the_adapter_and_ordered_after_it(unit):
    text = read(unit)
    assert "BindsTo=%s" % DEVICE_UNIT in text, unit
    assert "After=%s" % DEVICE_UNIT in text, unit


@pytest.mark.parametrize("unit", ("wall-line-in.service", "wall-kiosk-loop.service"))
def test_alsaloop_is_wrapped_in_the_bounded_guard(unit):
    text = read(unit)
    assert "wall-alsaloop-guard.py" in text
    assert "--max-errors" in text and "--window" in text
    # The guard must be the process systemd supervises, not something run after
    # alsaloop: a bare /usr/bin/alsaloop ExecStart is the wedge this fixes.
    exec_lines = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert exec_lines[0].startswith(
        "ExecStart=/usr/local/lib/wall-panel/wall-alsaloop-guard.py")
    # Restart= is the recovery mechanism the guard's exit hands back to, and a
    # start limit tighter than the guard's own reaction time would defeat it.
    assert "Restart=always" in text
    assert "StartLimitBurst=10" in text


def test_line_in_refuses_a_udev_start_when_it_is_disabled():
    """Enablement is the operator's off switch, and SYSTEMD_WANTS ignores it."""
    text = read("wall-line-in.service")
    condition = [ln for ln in text.splitlines() if ln.startswith("ExecCondition=")]
    assert len(condition) == 2
    # Order matters only for the reader: the enablement test is the first.
    assert "multi-user.target.wants/wall-line-in.service" in condition[0]


LINE_IN_MODE_GUARD = (
    "ExecCondition=/bin/sh -c '[ \"$(cat /etc/wall-panel/audio-mode "
    "2>/dev/null || echo trigger)\" != bus ]'")
VISUALIZER_MODE_GUARD = LINE_IN_MODE_GUARD.replace("!= bus", "= bus")


def _exec_conditions(unit):
    return [ln for ln in read(unit).splitlines() if ln.startswith("ExecCondition=")]


def test_line_in_refuses_to_start_in_bus_mode_only():
    """No linein_shared/speaker_shared pair in bus mode; panel mode keeps line-in."""
    assert LINE_IN_MODE_GUARD in _exec_conditions("wall-line-in.service")
    # The visualizer's line: same mode file, same absent-file default.
    assert VISUALIZER_MODE_GUARD in _exec_conditions("wall-bus-visualizer.service")


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on PATH")
@pytest.mark.parametrize("mode,exit_code", [
    ("bus", 1), ("trigger", 0), ("panel", 0), (None, 0)])
def test_line_in_mode_guard_exit_status(tmp_path, mode, exit_code):
    """Run the guard's own sh body: only bus skips; an absent file means trigger."""
    line = _exec_conditions("wall-line-in.service")[-1]
    assert line == LINE_IN_MODE_GUARD
    body = line[len("ExecCondition=/bin/sh -c "):].strip("'")
    mode_file = tmp_path / "audio-mode"
    if mode is not None:
        mode_file.write_text(mode + "\n")
    body = body.replace("/etc/wall-panel/audio-mode", str(mode_file).replace("\\", "/"))
    assert str(mode_file).replace("\\", "/") in body
    done = subprocess.run(["sh", "-c", body], capture_output=True, text=True)
    assert done.returncode == exit_code, done.stderr


def test_kiosk_loop_refuses_to_run_in_panel_mode():
    """udev's SYSTEMD_WANTS ignores enablement, so the mode is checked again."""
    text = read("wall-kiosk-loop.service")
    assert "ExecCondition=" in text
    assert "/etc/wall-panel/audio-mode" in text


# Lines systemd-analyze emits about THIS host rather than about these files:
# binaries and a device unit that exist only on the panel, and the executable
# bit every file carries on a Windows mount. Anything else is a defect.
HOST_ARTIFACTS = (
    re.compile(r"marked (executable|world-writable)"),
    re.compile(r"Command .*(wall-alsaloop-guard\.py|alsaloop|amixer|python3)"),
    re.compile(r"(wall-alsaloop-guard\.py|/usr/bin/alsaloop|/usr/bin/amixer|"
               r"/usr/bin/python3|/usr/local/lib/wall-panel)"),
    re.compile(r"dev-wall_(audio_adapter|amp_relay)\.device"),
    re.compile(r"Unit .*\.device .*(not found|Invalid argument)"),
)


def test_the_amp_unit_is_deliberately_started_even_in_panel_mode():
    """A replug starts it whatever the mode, and that is the safe direction.

    SYSTEMD_WANTS ignores enablement, so a panel-mode replug starts this unit.
    It is left that way on purpose: the daemon's first act is to command and
    VERIFY the relay OFF, and only then idle. Not starting it would leave a
    latching relay in whatever state it was in. The unit therefore must NOT
    grow the kiosk loop's mode ExecCondition.
    """
    text = read("wall-amp-trigger.service")
    assert "ExecCondition=" not in text
    assert "idl" in text.lower() or "verified OFF" in text


def test_reassert_and_relay_aliases_recover_without_a_stale_udev_leg_list():
    reassert = read("wall-audio-resume.service")
    assert "apply-state" in reassert
    assert "RemainAfterExit" not in reassert.split("[Service]", 1)[1]
    relay_rules = read("99-wall-amp-lcus2.rules")
    assert 'TAG+="systemd"' in relay_rules
    assert 'ENV{SYSTEMD_ALIAS}="/dev/wall_amp_relay"' in relay_rules
    assert 'ACTION=="add"' in relay_rules
    assert 'ENV{SYSTEMD_WANTS}+="wall-amp-trigger.service"' in relay_rules
    amp = read("wall-amp-trigger.service")
    assert "Wants=dev-wall_amp_relay.device" in amp
    assert "After=dev-wall_amp_relay.device" in amp
    assert "BindsTo=dev-wall_amp_relay.device" not in amp


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze is not available on this host")
def test_unit_files_pass_systemd_analyze_verify(tmp_path):
    verify_units = sorted(DIRECT_UNITS | {
        "wall-bus-speaker.service", "wall-speaker-out.service", "wall-mic-rear.service",
        "wall-audio-resume.service"})
    for unit in verify_units:
        shutil.copy(WALL / unit, tmp_path / unit)
        # The repo lives on a Windows mount where every file reads as 0777;
        # fixing the mode is better than filtering the complaint about it.
        (tmp_path / unit).chmod(0o644)
    got = subprocess.run(
        ["systemd-analyze", "verify", *[str(tmp_path / u) for u in verify_units]],
        capture_output=True, text=True)
    # Allowlist, not a denylist: an unrecognised diagnostic is a defect. The
    # [Service]-vs-[Unit] StartLimit mistake this caught reads as "Unknown key
    # name ... ignoring", and a denylist that happened to miss that phrasing
    # would have passed a unit whose restart bound silently did nothing.
    unexplained = [ln.strip() for ln in (got.stderr + got.stdout).splitlines()
                   if ln.strip()
                   and not any(p.search(ln) for p in HOST_ARTIFACTS)]
    assert not unexplained, unexplained
    # The exit status is evidence too, and ignoring it was terra's finding. On a
    # host that actually has the binaries these units name there is nothing left
    # to excuse a failure, so demand success outright; elsewhere a failure must
    # be accounted for by a host artifact that was actually printed.
    missing = [b for b in ("/usr/bin/alsaloop", "/usr/bin/amixer",
                           "/usr/bin/python3",
                           "/usr/local/lib/wall-panel/wall-alsaloop-guard.py")
               if not os.path.exists(b)]
    if not missing:
        assert got.returncode == 0, (got.returncode, got.stderr + got.stdout)
    elif got.returncode != 0:
        assert any(b in (got.stderr + got.stdout) for b in missing), (
            "systemd-analyze failed without naming a missing host binary: "
            + (got.stderr + got.stdout))


# --- the bounded recover loop ---------------------------------------------

def test_error_window_trips_only_past_the_cap():
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=3, window=10.0, sustain=1e9,
                               monotonic=lambda: clock[0])
    assert window.record() is False
    assert window.record() is False
    assert window.record() is False
    assert window.record() is True


def test_errors_spread_beyond_the_window_never_trip():
    """An isolated xrun an hour is a panel that works, not one to restart."""
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=3, window=10.0, sustain=30.0,
                               monotonic=lambda: clock[0])
    for _ in range(50):
        assert window.record() is False
        clock[0] += 11.0


def test_a_slow_but_unbroken_error_stream_still_trips():
    """The wedge the rate cap alone cannot see: one complaint a second, forever.

    terra found this: 20-in-10s is never reached at 1 Hz, so an alsaloop broken
    permanently would have run silent indefinitely while the unit read active --
    the exact failure this file exists to end, in slow motion.
    """
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=20, window=10.0, sustain=30.0,
                               monotonic=lambda: clock[0])
    tripped_at = None
    for _ in range(120):
        if window.record():
            tripped_at = clock[0]
            break
        clock[0] += 1.0
    assert tripped_at == 30.0
    assert "unbroken" in window.reason


def test_a_gap_of_exactly_the_window_is_a_clean_gap():
    """The boundary terra asked for: `window` seconds of quiet is quiet."""
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=20, window=10.0, sustain=30.0,
                               monotonic=lambda: clock[0])
    for _ in range(200):
        assert window.record() is False
        clock[0] += 10.0


def test_a_clean_gap_resets_the_sustained_streak():
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=20, window=10.0, sustain=30.0,
                               monotonic=lambda: clock[0])
    for _ in range(100):
        # 25 s of complaints, then quiet for longer than the window: never a
        # wedge, so it must never trip however long the panel runs.
        for _ in range(25):
            assert window.record() is False
            clock[0] += 1.0
        clock[0] += 11.0


def test_the_measured_message_is_what_trips_it():
    guard = load_guard()
    assert guard.is_error_line("alsaloop: unable to prepare slave")
    assert guard.is_error_line("Poll failed: Input/output error")
    assert not guard.is_error_line("Loop started")
    lines = ["unable to prepare slave"] * 5
    window = guard.ErrorWindow(max_errors=3, window=10.0, sustain=1e9)
    assert guard.watch(iter(lines), window) is True


def test_healthy_output_runs_to_the_end_without_tripping():
    guard = load_guard()
    window = guard.ErrorWindow(max_errors=3, window=10.0, sustain=1e9)
    assert guard.watch(iter(["Loop started", "sync 5", "done"]), window) is False


def test_guard_exits_nonzero_when_a_child_floods_the_error():
    """End to end with a stand-in for alsaloop: it never exits, the guard does."""
    guard_module = load_guard()
    child = ("import sys,time\n"
             "while True:\n"
             "    sys.stderr.write('unable to prepare slave\\n')\n"
             "    sys.stderr.flush()\n"
             "    time.sleep(0.001)\n")
    code = guard_module.main(["--max-errors", "5", "--window", "10",
                              "--", sys.executable, "-c", child])
    assert code == 1


def test_guard_passes_through_a_childs_own_exit_code():
    guard_module = load_guard()
    code = guard_module.main(["--", sys.executable, "-c",
                              "import sys; sys.exit(7)"])
    assert code == 7


def test_guard_reports_a_command_it_cannot_run():
    guard_module = load_guard()
    missing = str(Path(tempfile.gettempdir()) / "no-such-alsaloop-xyzzy")
    assert guard_module.main(["--", missing]) == 1


# --- respawn mode: the guard's own restart schedule (SR-082, 2026-10-16) ----
#
# Measured on the panel 2026-10-16: systemd's restart counter is never reset by
# a healthy run, so wall-spdif-in's RestartSteps ramp climbed during one bad
# patch and then charged every later glitch its 120 s ceiling (a crash after
# seventeen clean minutes: 104 s of silence). These pin the schedule that
# replaces it and the unit's use of it.

SPDIF_UNIT = "wall-spdif-in.service"


def _exec_start_argv(unit):
    """The unit's ExecStart as argv, continuation lines joined."""
    import shlex
    text = read(unit)
    joined = re.sub(r"\\\n[ \t]*", " ", text)
    line = [ln for ln in joined.splitlines() if ln.startswith("ExecStart=")]
    assert len(line) == 1, unit
    return shlex.split(line[0][len("ExecStart="):])


def test_backoff_ramps_on_fast_failures_and_holds_its_last_step_sr082():
    guard = load_guard()
    backoff = guard.Backoff((1, 2, 4), healthy_after=20)
    assert [backoff.next_delay(0.5) for _ in range(6)] == [1, 2, 4, 4, 4, 4]


def test_backoff_resets_after_a_healthy_run_however_high_it_climbed_sr082():
    """The defect itself: after a bad patch, a clean run must buy back delays[0]."""
    guard = load_guard()
    backoff = guard.Backoff((1, 2, 4, 8, 16, 30), healthy_after=20)
    for _ in range(10):
        backoff.next_delay(0.5)
    assert backoff.next_delay(0.5) == 30
    # Seventeen clean minutes, then the crash: back in a second, not at the cap.
    assert backoff.next_delay(17 * 60) == 1
    assert backoff.next_delay(0.5) == 2


def test_backoff_refuses_a_schedule_it_cannot_follow_sr082():
    guard = load_guard()
    for delays, healthy in (((), 20), ((1, -1), 20), ((1,), 0)):
        with pytest.raises(ValueError):
            guard.Backoff(delays, healthy)


class _FakeStop:
    """A stop event whose wait() records the delay and advances a fake clock."""

    def __init__(self, clock, stop_after_waits):
        self.clock = clock
        self.waits = []
        self._left = stop_after_waits
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, delay):
        self.waits.append(delay)
        self.clock[0] += delay
        self._left -= 1
        if self._left <= 0:
            self._set = True
        return self._set


def test_supervise_follows_the_schedule_between_child_runs_sr082():
    guard = load_guard()
    clock = [0.0]
    # Two fast failures, one long healthy run that then crashes, one fast failure.
    durations = iter([0.5, 0.5, 600.0, 0.5])

    def run_once():
        clock[0] += next(durations)
        return 1

    stop = _FakeStop(clock, stop_after_waits=4)
    code = guard.supervise(run_once, guard.Backoff((1, 2, 4, 8, 16, 30), 20),
                           stop, monotonic=lambda: clock[0])
    assert code == 0
    assert stop.waits == [1, 2, 1, 2]


def test_supervise_does_not_relaunch_after_a_stop_during_a_run_sr082():
    guard = load_guard()
    clock = [0.0]
    stop = _FakeStop(clock, stop_after_waits=99)
    runs = []

    def run_once():
        runs.append(1)
        stop.set()          # systemctl stop arrived while the child ran
        return 143

    assert guard.supervise(run_once, guard.Backoff(), stop,
                           monotonic=lambda: clock[0]) == 0
    assert runs == [1] and stop.waits == []


def test_a_stop_during_a_long_backoff_ends_supervise_at_once_sr082():
    """`systemctl stop` must not wait out a 30 s step (a plain sleep would)."""
    import threading
    import time
    guard = load_guard()
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        assert guard.supervise(lambda: 1, guard.Backoff((30,), 20), stop) == 0
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5


def test_respawn_relaunches_a_real_child_until_stopped_sr082(tmp_path):
    """Real processes, real Popen: each exit is followed by a relaunch."""
    import threading
    guard = load_guard()
    marker = tmp_path / "runs"
    child = ("import sys\n"
             "open(sys.argv[1], 'a').write('x')\n"
             "sys.stderr.write('Poll FD initialization failed.\\n')\n"
             "sys.exit(1)\n")
    command = [sys.executable, "-c", child, str(marker)]
    stop = threading.Event()
    holder = [None]
    codes = []

    def run_once():
        code = guard.run_child(command, guard.ErrorWindow(), holder, stop)
        codes.append(code)
        if len(codes) == 3:
            stop.set()
        return code

    assert guard.supervise(run_once, guard.Backoff((0.01,), 100), stop) == 0
    assert codes == [1, 1, 1]
    assert marker.read_text() == "xxx"
    assert holder == [None]


def test_respawn_mode_reports_a_command_it_cannot_run_sr082():
    """An unexecutable child is a broken install, not a stream fault: exit to systemd."""
    guard_module = load_guard()
    missing = str(Path(tempfile.gettempdir()) / "no-such-alsaloop-xyzzy")
    assert guard_module.main(["--respawn", "--respawn-delays", "0.01",
                              "--", missing]) == 1


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX SIGTERM delivery")
def test_sigterm_ends_the_respawn_loop_and_its_child_sr082(tmp_path):
    import signal
    import time
    pidfile = tmp_path / "child.pid"
    child = ("import os,sys,time\n"
             "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
             "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(GUARD), "--respawn",
                             "--respawn-delays", "0.01", "--",
                             sys.executable, "-c", child, str(pidfile)])
    deadline = time.time() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.time() < deadline, "child never started"
        time.sleep(0.05)
    child_pid = int(pidfile.read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    with pytest.raises(OSError):
        os.kill(child_pid, 0)


def test_the_spdif_unit_argv_is_one_the_guard_accepts_sr082():
    """Parse the unit's real ExecStart with the guard's real parser."""
    guard = load_guard()
    argv = _exec_start_argv(SPDIF_UNIT)
    assert argv[0] == "/usr/local/lib/wall-panel/wall-alsaloop-guard.py"
    args = guard.build_parser().parse_args(argv[1:])
    assert args.respawn is True
    backoff = guard.Backoff(args.respawn_delays, args.healthy_after)
    # The requirement's policy, not the constants: a glitch after a healthy run
    # is relaunched within 2 s, and a source that stays absent is retried at
    # least every 30 s, so a waking desktop is heard within half a minute.
    assert backoff.delays[0] <= 2
    assert max(backoff.delays) <= 30
    assert list(backoff.delays) == sorted(backoff.delays)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    assert command[0] == "/usr/bin/alsaloop"
    assert "--cdevice" in command and "spdif_in" in command


def test_only_the_external_source_leg_respawns_inside_the_guard_sr082():
    """Scope: panel-internal legs keep exiting to systemd (SR-037's boundary)."""
    users = sorted(p.name for p in WALL.glob("*.service")
                   if "wall-alsaloop-guard.py" in p.read_text(encoding="utf-8"))
    assert SPDIF_UNIT in users and len(users) > 1
    for name in users:
        if name != SPDIF_UNIT:
            assert "--respawn" not in _exec_start_argv(name), name
