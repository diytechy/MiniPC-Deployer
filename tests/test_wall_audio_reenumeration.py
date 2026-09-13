"""The audio units follow the USB adapter through a re-enumeration.

Owner item 25 (2026-09-13): the hub was moved to another port, the adapter came
back with the same card id, and the three long-running audio consumers kept
handles on the dead device instance. These tests pin the three mechanisms that
were added: the port-independent udev alias, the BindsTo= on it, and the bounded
alsaloop recover loop.
"""

import importlib.util
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
UNITS = ("wall-line-in.service", "wall-kiosk-loop.service",
         "wall-amp-trigger.service")
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
    body = "\n".join(line for line in RULES.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    for unit in UNITS:
        assert 'ENV{SYSTEMD_WANTS}+="%s"' % unit in body, unit


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

@pytest.mark.parametrize("unit", UNITS)
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


def test_kiosk_loop_refuses_to_run_in_panel_mode():
    """udev's SYSTEMD_WANTS ignores enablement, so the mode is checked again."""
    text = read("wall-kiosk-loop.service")
    assert "ExecCondition=" in text
    assert "/etc/wall-panel/audio-mode" in text


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze is not available on this host")
def test_unit_files_pass_systemd_analyze_verify(tmp_path):
    for unit in UNITS:
        shutil.copy(WALL / unit, tmp_path / unit)
    got = subprocess.run(
        ["systemd-analyze", "verify", *[str(tmp_path / u) for u in UNITS]],
        capture_output=True, text=True)
    noise = ("Unknown", "not found", "Failed to prepare", "ignoring")
    real = [ln for ln in (got.stderr + got.stdout).splitlines()
            # A unit that names a device unit no test host has, and the guard
            # path that exists only on the panel, are not defects here.
            if ln.strip() and "wall_audio_adapter" not in ln
            and "wall-alsaloop-guard" not in ln
            and "/usr/local/lib/wall-panel" not in ln
            and "/usr/bin/alsaloop" not in ln
            and "/usr/bin/amixer" not in ln
            and "/usr/lib/systemd" not in ln
            # The worktree lives on a Windows mount in CI, where every file
            # reads as 0777 and systemd calls that "marked executable".
            and "marked executable" not in ln]
    assert not [ln for ln in real if any(n in ln for n in noise)], real


# --- the bounded recover loop ---------------------------------------------

def test_error_window_trips_only_past_the_cap():
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=3, window=10.0,
                               monotonic=lambda: clock[0])
    assert window.record() is False
    assert window.record() is False
    assert window.record() is False
    assert window.record() is True


def test_errors_spread_beyond_the_window_never_trip():
    guard = load_guard()
    clock = [0.0]
    window = guard.ErrorWindow(max_errors=3, window=10.0,
                               monotonic=lambda: clock[0])
    for _ in range(50):
        assert window.record() is False
        clock[0] += 11.0


def test_the_measured_message_is_what_trips_it():
    guard = load_guard()
    assert guard.is_error_line("alsaloop: unable to prepare slave")
    assert guard.is_error_line("Poll failed: Input/output error")
    assert not guard.is_error_line("Loop started")
    lines = ["unable to prepare slave"] * 5
    window = guard.ErrorWindow(max_errors=3, window=10.0)
    assert guard.watch(iter(lines), window) is True


def test_healthy_output_runs_to_the_end_without_tripping():
    guard = load_guard()
    window = guard.ErrorWindow(max_errors=3, window=10.0)
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
