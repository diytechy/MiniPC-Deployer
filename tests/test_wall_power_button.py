"""SR-093/SR-094 (OfficeWallNaglight WSN-098): the panel's power button asks
before it shuts the panel down -- one press warns, a second within 5 s shuts
down, otherwise nothing happens.

Layers, each against the real artefact:

* TC-1315..TC-1322 -- the pure state machine in wall-power-button.py, driven
  with explicit boot-clock instants, for every case the plan's Done-when names:
  one press then timeout, two presses inside 5 s, two presses 6 s apart, one
  press reported on two devices, two quick presses on one device, autorepeat,
  a press at service start, and the kiosk unavailable.
* TC-1323 -- the Shell that carries out the machine's actions: the wake is
  off the press path, a failed poweroff stands down, alerts reach the journal
  and the hook point.
* TC-1324 -- the IF-067 files: the state document and the bounded ack read.
* TC-1325 -- the input layer and the event loop end to end, with the measured
  2026-10-22 event4 capture as the input bytes and real pipes as the devices.
* TC-1326 -- the shipped unit, the knob and logind's configuration.
* TC-1327 -- the firstboot block, cut out between its BEGIN/END markers and
  EXECUTED in bash under set -euo pipefail with every external command a
  recording function. Needs bash.
(TC-1328, the wall-sleep.sh arm, is A27 of
stack/autoinstall/wall/tests/occupancy-power.test.sh.)

WHAT THIS DOES NOT PROVE. Nothing here runs logind, systemd-inhibit or a real
power button. That logind honours a sessionless system service's
handle-power-key block inhibitor, that the installed unit holds it, that
killing the service releases it, and that a later press is then logind's
again, are the installed-panel checks (systemd-inhibit --list) and the Owner's
physical presses.

Implements tests: TC-1315, TC-1316, TC-1317, TC-1318, TC-1319, TC-1320, TC-1321,
TC-1322, TC-1323, TC-1324, TC-1325, TC-1326, TC-1327.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _load():
    spec = importlib.util.spec_from_file_location("wall_power_button", WALL / "wall-power-button.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pb = _load()
EV_SYN = 0
A, B = "/dev/input/event4", "/dev/input/event0"


def machine(**kwargs):
    return pb.PowerButtonMachine(seq_start=100, **kwargs)


def press(m, now, device=A):
    """One measured press: value 1 then value 0 at the same instant."""
    actions = m.key_event(device, pb.EV_KEY, pb.KEY_POWER, 1, now)
    actions += m.key_event(device, pb.EV_KEY, pb.KEY_POWER, 0, now)
    return actions


def kinds(actions):
    return [action[0] for action in actions]


def published(actions):
    return [action[1]["state"] for action in actions if action[0] == "publish"]


def poweroffs(actions):
    return kinds(actions).count("poweroff")


def no_ack():
    return None


# ── TC-1315: one press, then the window runs out ─────────────────────────────

def test_tc1315_one_press_then_timeout_warns_and_never_shuts_down():
    m = machine()
    first = press(m, 10.0)
    assert published(first) == ["armed"] and "wake" in kinds(first)
    assert first[0][1] == {"state": "armed", "seq": 101, "remainingS": 5.0}
    assert poweroffs(first) == 0
    assert m.tick(14.9, lambda: 101) == []            # still inside the window
    assert m.tick(15.2, lambda: 101) == []            # the expiry grace for a late-read press
    cleared = m.tick(15.26, lambda: 101)
    assert published(cleared) == ["idle"] and poweroffs(cleared) == 0
    assert m.state == pb.IDLE and m.next_due() is None


def test_tc1315_the_loop_is_woken_for_the_ack_check_and_the_window_end():
    m = machine()
    press(m, 10.0)
    assert m.next_due() == pytest.approx(12.0)        # the acknowledgement check first
    m.tick(12.0, lambda: 101)
    assert m.next_due() == pytest.approx(15.251)      # then just past the window and its grace


# ── TC-1316: two presses inside 5 s ──────────────────────────────────────────

@pytest.mark.parametrize("gap", [0.5, 2.0, 4.99, 5.0])
def test_tc1316_a_second_press_inside_5_s_requests_shutdown(gap):
    m = machine()
    press(m, 10.0)
    second = press(m, 10.0 + gap)
    assert poweroffs(second) == 1
    assert published(second) == ["shutting-down"]
    assert m.state == pb.SHUTTING_DOWN


def test_tc1316_a_late_read_press_is_judged_on_its_own_timestamp():
    # Sol review 1: the window used to expire at the processing instant before
    # waiting input was read, turning an in-window second press into a warning.
    m = machine()
    press(m, 10.0)
    assert m.tick(15.1, lambda: 101) == []             # the loop woke just past the deadline
    assert poweroffs(press(m, 14.99)) == 1             # the press it then reads was in time


def test_tc1316_presses_after_the_shutdown_request_do_nothing():
    m = machine()
    press(m, 10.0)
    press(m, 11.0)
    assert press(m, 11.5) == [] and press(m, 12.0, B) == []


# ── TC-1317: two presses 6 s apart ───────────────────────────────────────────

def test_tc1317_two_presses_6_s_apart_are_two_warnings_and_no_shutdown():
    m = machine()
    press(m, 10.0)
    second = press(m, 16.0)                       # no tick in between: the press clears it
    assert poweroffs(second) == 0
    assert published(second) == ["idle", "armed"]
    assert m.seq == 102 and m.deadline == pytest.approx(21.0)
    third = press(m, 22.5)
    assert poweroffs(third) == 0 and m.seq == 103


def test_tc1317_just_past_the_window_is_a_new_warning():
    m = machine()
    press(m, 10.0)
    assert poweroffs(press(m, 15.001)) == 0


# ── TC-1318: one press reported on two devices is one press ──────────────────

@pytest.mark.parametrize("echo", [0.0, 0.02, 0.149])
def test_tc1318_one_press_on_two_devices_counts_once(echo):
    m = machine()
    press(m, 10.0, A)
    assert press(m, 10.0 + echo, B) == []
    assert m.state == pb.ARMED and m.seq == 101


def test_tc1318_the_echo_of_a_second_press_is_also_dropped_and_still_shuts_down_once():
    m = machine()
    press(m, 10.0, A)
    press(m, 10.03, B)                            # echo of press 1
    second = press(m, 11.0, B)                    # press 2 arrives on B first this time
    assert poweroffs(second) == 1
    assert press(m, 11.02, A) == []               # its echo


def test_tc1318_presses_on_different_devices_far_apart_are_two_presses():
    m = machine()
    press(m, 10.0, A)
    assert poweroffs(press(m, 10.5, B)) == 1


# ── TC-1319: two quick presses on one device count twice ─────────────────────

@pytest.mark.parametrize("gap", [0.0, 0.05, 0.12, 0.299])
def test_tc1319_two_quick_presses_on_one_device_are_two_presses(gap):
    m = machine()
    press(m, 10.0, A)
    assert poweroffs(press(m, 10.0 + gap, A)) == 1


# ── TC-1320: only a press counts ─────────────────────────────────────────────

@pytest.mark.parametrize("etype,code,value", [
    (pb.EV_KEY, pb.KEY_POWER, 2),          # autorepeat
    (pb.EV_KEY, pb.KEY_POWER, 0),          # release
    (EV_SYN, 0, 0),                        # sync report
    (pb.EV_KEY, 142, 1),                   # KEY_SLEEP
    (pb.EV_KEY, 115, 1),                   # KEY_VOLUMEUP
])
def test_tc1320_autorepeat_releases_and_other_keys_are_ignored(etype, code, value):
    m = machine()
    assert m.key_event(A, etype, code, value, 10.0) == []
    assert m.state == pb.IDLE


def test_tc1320_autorepeat_inside_a_warning_is_not_a_second_press():
    m = machine()
    m.key_event(A, pb.EV_KEY, pb.KEY_POWER, 1, 10.0)
    for step in range(1, 30):
        assert m.key_event(A, pb.EV_KEY, pb.KEY_POWER, 2, 10.0 + step * 0.033) == []
    assert m.state == pb.ARMED


# ── TC-1321: a press at service start ────────────────────────────────────────

def test_tc1321_start_publishes_idle_and_the_first_press_only_warns():
    m = machine()
    assert m.start(0.0) == [("publish", {"state": "idle", "seq": 100, "remainingS": None})]
    first = press(m, 0.0)                          # the very first event of the run
    assert poweroffs(first) == 0 and published(first) == ["armed"]


def test_tc1321_a_stale_armed_file_from_a_previous_run_is_overwritten_at_start(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"schemaVersion": 1, "state": "armed", "seq": 5}), encoding="utf-8")
    m = machine()
    shell = pb.Shell(m, write=lambda doc: pb.write_state(doc, state), log=lambda *a: None,
                     wall_ms=lambda: 1_000, spawn=lambda target: None)
    shell.execute(m.start(0.0), 0.0)
    assert json.loads(state.read_text(encoding="utf-8"))["state"] == "idle"


def test_tc1321_sequence_numbers_never_repeat_across_runs():
    first = pb.PowerButtonMachine(seq_start=1_000)
    press(first, 1.0)
    later = pb.PowerButtonMachine(seq_start=2_000)
    press(later, 1.0)
    assert later.seq != first.seq


# ── TC-1322: kiosk unavailable ───────────────────────────────────────────────

def test_tc1322_no_acknowledgement_raises_an_alert_and_keeps_guarding():
    m = machine()
    press(m, 10.0)
    actions = m.tick(12.0, no_ack)
    assert [a[:2] for a in actions] == [("alert", "power-button-kiosk-unavailable")]
    assert "still guarded" in actions[0][2]
    assert m.state == pb.ARMED                      # the window is untouched
    assert poweroffs(press(m, 13.0)) == 1           # and a blind double press shuts down


def test_tc1322_an_acknowledgement_for_the_current_warning_raises_nothing():
    m = machine()
    press(m, 10.0)
    assert m.tick(12.0, lambda: 101) == []


def test_tc1322_an_acknowledgement_for_an_older_warning_is_no_acknowledgement():
    m = machine()
    press(m, 10.0)
    m.tick(16.0, lambda: 101)
    press(m, 20.0)                                  # warning 102
    actions = m.tick(22.0, lambda: 101)
    assert kinds(actions) == ["alert"]


def test_tc1322_the_ack_is_read_once_and_only_when_due():
    m = machine()
    reads = []
    press(m, 10.0)
    m.tick(11.0, lambda: reads.append(1))
    assert reads == []
    m.tick(12.0, lambda: reads.append(1))
    m.tick(13.0, lambda: reads.append(1))
    assert reads == [1]


def test_tc1322_a_kiosk_that_never_paints_still_gets_the_window_closed():
    m = machine()
    press(m, 10.0)
    actions = m.tick(15.5, no_ack)                  # check and window due in one tick
    assert kinds(actions)[0] == "alert" and published(actions) == ["idle"]


def test_tc1322_the_daemon_has_no_path_that_releases_the_inhibitor():
    # The inhibitor is the systemd-inhibit wrapper's and lives as long as the
    # process: the daemon must never exit on a kiosk fault, and holds no
    # inhibitor code of its own that could drop it early.
    source = (WALL / "wall-power-button.py").read_text(encoding="utf-8")
    assert "Inhibit" not in re.sub(r'"""(.|\n)*?"""', "", source)
    m = machine()
    shell = Recorder(m)
    press(m, 10.0)
    shell.execute(m.tick(12.0, no_ack), 12.0)
    shell.run_spawned()
    assert m.state == pb.ARMED
    assert any("ALERT power-button-kiosk-unavailable" in line for line, _ in shell.lines)


# ── TC-1323: the Shell ───────────────────────────────────────────────────────

class Recorder(pb.Shell):
    def __init__(self, m, *, returncode=0, hook=None, run_error=None):
        self.commands, self.lines, self.written, self.spawned = [], [], [], []

        def run(argv, **kwargs):
            self.commands.append((tuple(argv), kwargs.get("timeout")))
            if run_error:
                raise run_error
            return subprocess.CompletedProcess(argv, returncode)

        super().__init__(m, run=run, write=self.written.append,
                         log=lambda line, priority=6: self.lines.append((line, priority)),
                         wall_ms=lambda: 1_700_000_000_000,
                         alert_hook=hook or Path("/nonexistent/panel-alert"),
                         spawn=self.spawned.append)

    def run_spawned(self):
        while self.spawned:
            self.spawned.pop(0)()


def test_tc1323_the_wake_never_runs_on_the_press_path():
    m = machine()
    shell = Recorder(m)
    shell.execute(press(m, 10.0), 10.0)
    assert shell.commands == [] and len(shell.spawned) == 1
    shell.run_spawned()
    assert shell.commands == [(pb.WAKE_COMMAND, pb.WAKE_TIMEOUT_S)]


def test_tc1323_a_failed_wake_alerts_and_changes_nothing_else():
    m = machine()
    shell = Recorder(m, returncode=1)
    shell.execute(press(m, 10.0), 10.0)
    shell.run_spawned()
    assert any(line.startswith("ALERT power-button-wake-failed") and priority == 4
               for line, priority in shell.lines)
    assert m.state == pb.ARMED


def test_tc1323_the_second_press_runs_systemctl_poweroff():
    m = machine()
    shell = Recorder(m)
    shell.execute(press(m, 10.0), 10.0)
    shell.spawned.clear()
    shell.execute(press(m, 11.0), 11.0)
    assert shell.commands == [(("/usr/bin/systemctl", "poweroff"), pb.POWEROFF_TIMEOUT_S)]
    assert shell.written[-1]["state"] == "shutting-down"


@pytest.mark.parametrize("returncode,error", [(1, None), (0, subprocess.TimeoutExpired("x", 1))])
def test_tc1323_a_failed_poweroff_stands_down_to_idle_and_alerts(returncode, error):
    m = machine()
    shell = Recorder(m, returncode=returncode, run_error=error)
    shell.execute(press(m, 10.0), 10.0)
    shell.spawned.clear()
    shell.execute(press(m, 11.0), 11.0)
    assert m.state == pb.IDLE and shell.written[-1]["state"] == "idle"
    shell.run_spawned()
    assert any(line.startswith("ALERT power-button-poweroff-failed") for line, _ in shell.lines)
    assert poweroffs(press(m, 12.0)) == 0           # back to warning first


def test_tc1323_a_shutdown_that_never_happens_hands_the_button_back():
    m = machine(stall_s=60.0)
    press(m, 10.0)
    press(m, 11.0)
    assert m.next_due() == pytest.approx(71.0)
    actions = m.tick(71.0, no_ack)
    assert published(actions) == ["idle"] and kinds(actions)[-1] == "alert"
    assert poweroffs(press(m, 80.0)) == 0


def test_tc1323_without_a_hook_the_alert_is_journal_only_and_says_so():
    m = machine()
    shell = Recorder(m)
    shell.alert("k", "message")
    assert shell.lines[0] == ("ALERT k: message", 4)
    assert shell.lines[1][0].startswith("ALERT NOT DELIVERED k: no panel alert route")
    assert shell.commands == []


def test_tc1323_an_installed_hook_is_called_with_the_key_and_message(tmp_path):
    hook = tmp_path / "panel-alert"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    if not os.access(hook, os.X_OK):
        pytest.skip("this filesystem cannot mark a file executable")
    m = machine()
    shell = Recorder(m, hook=hook)
    shell.alert("k", "message")
    assert shell.commands == [((str(hook), "k", "message"), pb.ALERT_TIMEOUT_S)]
    assert not any("NOT DELIVERED" in line for line, _ in shell.lines)


@pytest.mark.parametrize("returncode,error,expected", [
    (3, None, "alert k not re-sent: the hub was told less than 30 min ago"),
    (4, None, "ALERT NOT DELIVERED k: the alert route could not reach the hub (exit 4)"),
    (0, subprocess.TimeoutExpired("panel-alert", 10),
     "ALERT NOT DELIVERED k: the alert route could not reach the hub (TimeoutExpired)"),
])
def test_tc1323_the_route_outcome_is_journaled(tmp_path, returncode, error, expected):
    hook = tmp_path / "panel-alert"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    if not os.access(hook, os.X_OK):
        pytest.skip("this filesystem cannot mark a file executable")
    shell = Recorder(machine(), hook=hook, returncode=returncode, run_error=error)
    shell.alert("k", "message")
    assert shell.lines[0] == ("ALERT k: message", 4)
    assert any(line.startswith(expected) for line, _ in shell.lines[1:]), shell.lines


def test_tc1323_an_unknown_action_is_refused_not_dropped():
    with pytest.raises(ValueError):
        Recorder(machine()).execute([("reboot",)], 0.0)


def test_tc1323_a_state_file_that_cannot_be_written_is_journaled_and_the_guard_stays():
    m = machine()
    shell = Recorder(m)

    def refuse(_doc):
        raise PermissionError("no")

    shell.write = refuse
    shell.execute(press(m, 10.0), 10.0)
    assert m.state == pb.ARMED
    assert any("could not write the warning state" in line for line, _ in shell.lines)


# ── TC-1324: the IF-067 files ────────────────────────────────────────────────

def test_tc1324_the_state_document_carries_the_contract_fields():
    armed = pb.state_document({"state": "armed", "seq": 7, "remainingS": 4.25}, 1_000_000)
    assert armed == {"schemaVersion": 1, "state": "armed", "seq": 7, "windowMs": 5000,
                     "deadline": 1_004_250, "observedAt": 1_000_000}
    idle = pb.state_document({"state": "idle", "seq": 7, "remainingS": None}, 1_000_000)
    assert idle["deadline"] is None and idle["state"] == "idle"


def test_tc1324_write_state_replaces_atomically_and_is_world_readable(tmp_path):
    target = tmp_path / "run" / "state.json"
    pb.write_state({"a": 1}, target)
    pb.write_state({"a": 2}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("content,expected", [
    ('{"schemaVersion":1,"seq":42,"shownAt":1}', 42),
    ('{"seq":true}', None),
    ('{"seq":"42"}', None),
    ('[42]', None),
    ('{', None),
    ('{"seq":1,"pad":"' + "x" * 5000 + '"}', None),
    (b'\xff\xfe', None),
])
def test_tc1324_the_ack_read_accepts_only_a_small_object_with_an_integer_seq(tmp_path, content, expected):
    path = tmp_path / "ack.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    assert pb.read_ack(path) == expected


def test_tc1324_a_missing_or_symlinked_ack_is_no_acknowledgement(tmp_path):
    assert pb.read_ack(tmp_path / "absent.json") is None
    real = tmp_path / "real.json"
    real.write_text('{"seq":9}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("no O_NOFOLLOW on this platform")
    assert pb.read_ack(link) is None


def test_tc1324_the_paths_are_the_contract():
    assert pb.STATE_FILE == Path("/run/wall-power-button/state.json")
    assert pb.ACK_FILE == Path("/run/wall-panel-renderer/power-warning-ack.json")
    tmpfiles = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert "d /run/wall-panel-renderer 0755 panel panel -" in tmpfiles


# ── TC-1325: the input layer and the loop ────────────────────────────────────

# The 2026-10-22 capture from event4 (panel /var/tmp/powerbtn-20261022-003205),
# decoded: each press is KEY_POWER 1, SYN, KEY_POWER 0, SYN, tens of
# microseconds apart.
MEASURED = [(1792647174, 223550, 1, 116, 1), (1792647174, 223550, 0, 0, 0),
            (1792647174, 223562, 1, 116, 0), (1792647174, 223562, 0, 0, 0),
            (1792647187, 242416, 1, 116, 1), (1792647187, 242416, 0, 0, 0),
            (1792647187, 242495, 1, 116, 0), (1792647187, 242495, 0, 0, 0)]


def encode(records):
    return b"".join(struct.pack(pb.EVENT_FORMAT, *record) for record in records)


def test_tc1325_the_measured_capture_parses_to_two_presses_13_s_apart():
    events = pb.parse_events(encode(MEASURED) + b"\x00" * 7)      # a torn tail is ignored
    assert pb.EVENT_SIZE == 24 and len(events) == 8
    presses = [stamp for etype, code, value, stamp in events if pb.is_press(etype, code, value)]
    assert len(presses) == 2 and presses[1] - presses[0] == pytest.approx(13.018866)


def test_tc1325_devices_are_found_by_their_exact_name(tmp_path):
    for node, name in (("event0", "Power Button"), ("event1", "Sleep Button"),
                       ("event4", "Power Button\n"), ("event11", "Intel Virtual Buttons"),
                       ("event12", "Power Button Extra")):
        (tmp_path / node / "device").mkdir(parents=True)
        (tmp_path / node / "device" / "name").write_text(name, encoding="utf-8")
    (tmp_path / "event9").mkdir()                                # no device/name at all
    assert pb.find_devices(tmp_path) == ["/dev/input/event0", "/dev/input/event4"]


def loop_rig(chunks_by_device):
    """Real pipes stand in for the device nodes. Each `wait` advances the clock
    by one second, then makes every device with a queued chunk ready: bytes are
    written to its pipe, None closes the writer (the device goes away)."""
    handles, writers = {}, {}
    for path in chunks_by_device:
        read_fd, write_fd = os.pipe()
        handles[read_fd] = (path, False)                          # read-time clock
        writers[path] = write_fd
    queued = {path: list(chunks) for path, chunks in chunks_by_device.items()}
    state = {"now": 0.0}

    def clock():
        return state["now"]

    def wait(fds, _w, _x, _timeout):
        state["now"] += 1.0
        ready = []
        for fd in fds:
            path = handles[fd][0]
            if queued[path]:
                chunk = queued[path].pop(0)
                if chunk is None:
                    os.close(writers[path])
                else:
                    os.write(writers[path], chunk)
                ready.append(fd)
        return ready, [], []

    return handles, wait, clock


def test_tc1325_the_loop_turns_the_measured_bytes_into_a_warning_then_a_shutdown():
    press_bytes = encode(MEASURED[:4])
    handles, wait, clock = loop_rig({A: [press_bytes, press_bytes, None]})
    m = machine()
    shell = Recorder(m)
    result = pb.run(handles, m, shell, clock=clock, wait=wait, ack=lambda: 101)
    states = [doc["state"] for doc in shell.written]
    assert states == ["idle", "armed", "shutting-down"]
    assert shell.commands == [(("/usr/bin/systemctl", "poweroff"), pb.POWEROFF_TIMEOUT_S)]
    assert result == 1 and handles == {}                         # every device gone: exit 1


def test_tc1325_losing_any_device_exits_for_a_restart_instead_of_guarding_a_dead_button():
    # Sol review 1: event4 is the only node that has ever reported a press, so
    # carrying on with event0 alone would hold the inhibitor over a dead button.
    handles, wait, clock = loop_rig({A: [None], B: [encode(MEASURED[:4])]})
    m = machine()
    shell = Recorder(m)
    assert pb.run(handles, m, shell, clock=clock, wait=wait, ack=no_ack) == 1
    assert handles == {}
    assert any("went away; exiting so the restart finds the power-button devices" in line
               for line, _ in shell.lines)


def test_tc1325_with_no_further_input_the_loop_itself_checks_the_ack_and_ends_the_window():
    # The timer half of the loop: one press, then every wait times out at the
    # instant the loop asked for. The ack check and the window's end must come
    # from the loop's own tick, not from a later press.
    read_fd, write_fd = os.pipe()
    handles = {read_fd: (A, False)}
    state = {"now": 0.0, "pressed": False, "timeouts": []}

    def wait(fds, _w, _x, timeout):
        if not state["pressed"]:
            state["pressed"] = True
            state["now"] = 10.0
            os.write(write_fd, encode(MEASURED[:4]))
            return list(fds), [], []
        state["timeouts"].append(timeout)
        state["now"] += timeout or 0.0
        return [], [], []

    m = machine()
    shell = Recorder(m)
    stops = {"n": 0}

    def stop():
        stops["n"] += 1
        return stops["n"] > 4

    pb.run(handles, m, shell, clock=lambda: state["now"], wait=wait, ack=no_ack, stop=stop)
    shell.run_spawned()
    os.close(read_fd); os.close(write_fd)
    assert [doc["state"] for doc in shell.written] == ["idle", "armed", "idle"]
    assert state["timeouts"][:2] == [pytest.approx(2.0), pytest.approx(3.251)]
    assert any("ALERT power-button-kiosk-unavailable" in line for line, _ in shell.lines)
    assert state["timeouts"][2] is None, "nothing to wait for once idle"


def test_tc1325_input_waiting_at_the_deadline_is_read_before_the_window_expires():
    # Kernel-stamped (boot-clock) events: the second press is stamped 14.99 s but
    # the loop only wakes at 15.5 s, past the deadline and its grace.
    def stamped(seconds):
        whole, frac = int(seconds), round((seconds % 1) * 1_000_000)
        return encode([(whole, frac, 1, 116, 1), (whole, frac, 0, 0, 0), (whole, frac, 1, 116, 0)])

    read_fd, write_fd = os.pipe()
    handles = {read_fd: (A, True)}
    script = [(10.0, stamped(10.0)), (15.5, stamped(14.99)), (16.0, None)]
    state = {"now": 0.0}

    def wait(fds, _w, _x, _timeout):
        state["now"], chunk = script.pop(0)
        if chunk is None:
            os.close(write_fd)
        else:
            os.write(write_fd, chunk)
        return list(fds), [], []

    m = machine()
    shell = Recorder(m)
    assert pb.run(handles, m, shell, clock=lambda: state["now"], wait=wait, ack=lambda: 101) == 1
    assert [doc["state"] for doc in shell.written] == ["idle", "armed", "shutting-down"]


def test_tc1317_a_suspend_inside_the_window_ends_it_and_the_next_press_only_warns(monkeypatch):
    # Sol review 2 (F1): the scheduled suspend can catch a warning. Across it the
    # boot clock gains the hours slept while the monotonic clock gains almost
    # nothing. The loop runs on its DEFAULT clock, with a device that refused
    # the kernel clock (stamped at read time), so the clock the daemon itself
    # reads decides: on CLOCK_MONOTONIC this press would shut the panel down.
    clocks = {"boot": 10.0, "mono": 10.0}

    def clock_gettime(clock_id):
        assert clock_id == 7, "the loop must read CLOCK_BOOTTIME"
        return clocks["boot"]

    monkeypatch.setattr(pb, "_BOOTTIME_CLOCK_ID", 7)
    monkeypatch.setattr(pb.time, "clock_gettime", clock_gettime, raising=False)
    monkeypatch.setattr(pb.time, "monotonic", lambda: clocks["mono"])
    read_fd, write_fd = os.pipe()
    handles = {read_fd: (A, False)}
    steps = ["press", "suspend-then-press", "gone"]

    def wait(fds, _w, _x, _timeout):
        step = steps.pop(0)
        if step == "gone":
            os.close(write_fd)
        else:
            if step == "suspend-then-press":           # 21:59:59 to 06:45, then a press
                clocks["boot"] += 8 * 3600 + 46 * 60
                clocks["mono"] += 1.0
            os.write(write_fd, encode(MEASURED[:4]))
        return list(fds), [], []

    m = machine()
    shell = Recorder(m)
    assert pb.run(handles, m, shell, wait=wait, ack=lambda: m.seq) == 1
    assert [doc["state"] for doc in shell.written] == ["idle", "armed", "idle", "armed"]
    assert pb.POWEROFF_COMMAND not in [argv for argv, _ in shell.commands]
    assert m.seq == 102, "the press after the suspend is a NEW warning"


def test_tc1325_each_device_is_asked_for_boot_clock_timestamps(tmp_path, monkeypatch):
    calls = []

    class FakeFcntl:
        @staticmethod
        def ioctl(fd, request, argument):
            calls.append((request, argument))

    node = tmp_path / "event4"
    node.write_bytes(b"")
    monkeypatch.setattr(pb, "fcntl", FakeFcntl)
    handles = pb.open_devices([str(node)])
    try:
        assert list(handles.values()) == [(str(node), True)]
        assert calls == [(pb.EVIOCSCLOCKID, struct.pack("i", 7))], "EVIOCSCLOCKID with CLOCK_BOOTTIME"
        assert pb.CLOCK_BOOTTIME == 7
    finally:
        for fd in handles:
            os.close(fd)


@pytest.mark.parametrize("failing", [A, B])
def test_tc1325_a_device_that_will_not_open_stops_the_guard_before_it_starts(monkeypatch, capsys, failing):
    # Sol review 2 (F2): event0 opens and event4 does not (or the other way
    # round). Guarding the subset would hold the inhibitor over a button that
    # reports on the missing node, so main exits non-zero for the restart,
    # closes what it opened, and never enters the loop.
    opened, closed = [], []
    real_close = os.close
    monkeypatch.setattr(pb, "find_devices", lambda: [B, A])

    def fake_open(path, flags):
        if path == failing:
            raise PermissionError(13, "Permission denied", path)
        read_fd, write_fd = os.pipe()
        real_close(write_fd)
        opened.append(read_fd)
        return read_fd

    def fake_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(pb.os, "open", fake_open)
    monkeypatch.setattr(pb.os, "close", fake_close)
    monkeypatch.setattr(pb, "fcntl", None)
    monkeypatch.setattr(pb, "run", lambda *a, **k: pytest.fail("run() entered with a device missing"))
    assert pb.main() == 77
    assert opened and sorted(closed) == sorted(opened)
    out = capsys.readouterr().out
    assert ("could not open %s (of %s, %s)" % (failing, B, A)) in out
    assert "exiting without guarding" in out


def test_tc1325_the_default_clock_is_the_boot_clock():
    assert pb.run.__kwdefaults__["clock"] is pb.boot_clock
    if hasattr(pb.time, "CLOCK_BOOTTIME"):
        assert pb.boot_clock() == pytest.approx(pb.time.clock_gettime(pb.time.CLOCK_BOOTTIME), abs=1.0)


# ── TC-1326: the unit, the knob, logind ──────────────────────────────────────

def _directives(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_tc1326_the_unit_runs_the_daemon_inside_a_handle_power_key_block_inhibitor():
    unit = _directives(WALL / "wall-power-button.service")
    [exec_start] = [line for line in unit if line.startswith("ExecStart=")]
    argv = exec_start[len("ExecStart="):]
    assert argv.startswith("/usr/bin/systemd-inhibit --what=handle-power-key --mode=block "
                           "--who=wall-power-button ")
    assert argv.endswith(" /usr/bin/python3 /usr/local/lib/wall-panel/wall-power-button.py")
    for directive in ("Restart=always", "RestartSec=2", "RuntimeDirectory=wall-power-button",
                      "RuntimeDirectoryMode=0755", "WantedBy=multi-user.target",
                      "After=systemd-logind.service"):
        assert directive in unit, directive
    assert not any(line.startswith(("User=", "DynamicUser=", "RuntimeDirectoryPreserve="))
                   for line in unit), "root, and no state document survives a stop"


def test_tc1326_logind_is_left_honouring_inhibitors_with_its_default_power_key():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    logind = re.search(r"cat > /etc/systemd/logind.conf.d/kiosk.conf <<'EOF'\n(.*?)\nEOF\n",
                       firstboot, re.S).group(1)
    for forbidden in ("PowerKeyIgnoreInhibited", "HandlePowerKey"):
        assert forbidden not in logind, forbidden


def test_tc1326_the_knob_is_documented_and_defaults_on():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    assert re.search(r"^WALL_POWER_BUTTON_GUARD=true$", env, re.M)
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert ': "${WALL_POWER_BUTTON_GUARD:=true}"' in firstboot


def test_tc1326_new_files_are_lf_and_the_daemon_is_executable_in_git():
    for name in ("wall-power-button.py", "wall-power-button.service", "panel-alert.sh",
                 "homehub-alert.sh"):
        assert b"\r" not in (WALL / name).read_bytes(), name
    for name in ("wall-power-button.py", "panel-alert.sh", "homehub-alert.sh"):
        out = subprocess.run(["git", "-C", str(WALL), "ls-files", "-s", name],
                             capture_output=True, text=True, check=True).stdout
        assert out.startswith("100755"), (name, out)


def test_tc1326_the_unit_may_reach_the_hub_and_keeps_the_route_state():
    unit = _directives(WALL / "wall-power-button.service")
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "StateDirectory=wall-panel-alert" in unit and "StateDirectoryMode=0700" in unit


# ── TC-1327: the firstboot block, executed ───────────────────────────────────

def block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN power-button-guard\n(.*?)# END power-button-guard\n", text, re.S)
    assert found, "wall-firstboot.sh lost its power-button-guard block"
    return found.group(1)


def run_block(tmp_path, *, knob="true", same=False, fail_install="", enable_status=0,
              inhibitor=True, restart_status=0):
    calls = tmp_path / "calls"
    rec = 'printf "%s\\n" "{kind} $*" >> "' + calls.as_posix() + '"'
    listing = ("wall-power-button 0 root 4242 systemd-inhibit handle-power-key "
               "The power button asks block") if inhibitor else "electron 1000 panel 9 electron sleep x delay"
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD="{}"'.format(WALL.as_posix()),
        "WALL_POWER_BUTTON_GUARD='{}'".format(knob) if knob is not None else "unset WALL_POWER_BUTTON_GUARD",
        'log() { echo "[wall-firstboot] $*"; }',
        'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }',
        'fail_step() { echo "[wall-firstboot] ERROR: $*" >&2; %s; }' % rec.format(kind="fail_step"),
        'install() { %s; case "$*" in *"%s"*) [ -n "%s" ] && return 1 ;; esac; return 0; }'
        % (rec.format(kind="install"), fail_install or "\x01", fail_install),
        'cmp() { return %d; }' % (0 if same else 1),
        'sleep() { :; }',
        'systemctl() { %s; [ "$1" = restart ] && return %d; return 0; }'
        % (rec.format(kind="systemctl"), restart_status),
        'systemd-inhibit() { %s; printf "%%s\\n" "%s"; }' % (rec.format(kind="systemd-inhibit"), listing),
        'enable_unit_now() { shift; %s; [ %d = 0 ] || { fail_step "systemctl enable --now FAILED for: $*"; return 1; }; }'
        % (rec.format(kind="enable_unit_now"), enable_status),
        block(),
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          env=dict(os.environ), text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


INSTALLS = [
    "install -d -m 0755 /usr/local/lib/wall-panel",
    "install -m 0755 {p}/wall-power-button.py /usr/local/lib/wall-panel/wall-power-button.py",
    "install -m 0644 {p}/wall-power-button.service /etc/systemd/system/wall-power-button.service",
]


def _ok(done):
    assert done.returncode == 0 and "BLOCK-DONE" in done.stdout, done.stdout + done.stderr


def _fails(calls):
    return [c for c in calls if c.startswith("fail_step")]


def _actions(calls):
    return [c for c in calls if c.startswith(("systemctl", "enable_unit_now", "systemd-inhibit"))]


@needs_bash
def test_tc1327_knob_on_installs_starts_restarts_a_changed_unit_and_checks_the_inhibitor(tmp_path):
    done, calls = run_block(tmp_path)
    _ok(done)
    p = WALL.as_posix()
    assert [c for c in calls if c.startswith("install")] == [i.format(p=p) for i in INSTALLS]
    assert _actions(calls) == ["systemctl daemon-reload", "enable_unit_now wall-power-button.service",
                               "systemctl restart wall-power-button.service",
                               "systemd-inhibit --list --no-pager --no-legend"]
    assert _fails(calls) == []
    assert "handle-power-key inhibitor held by wall-power-button" in done.stdout


@needs_bash
def test_tc1327_an_unchanged_unit_is_not_restarted(tmp_path):
    done, calls = run_block(tmp_path, same=True)
    _ok(done)
    assert "systemctl restart wall-power-button.service" not in calls


@needs_bash
def test_tc1327_the_default_is_on(tmp_path):
    done, calls = run_block(tmp_path, knob=None)
    _ok(done)
    assert "enable_unit_now wall-power-button.service" in calls


@needs_bash
def test_tc1327_no_inhibitor_after_start_is_a_failed_step(tmp_path):
    done, calls = run_block(tmp_path, inhibitor=False)
    _ok(done)
    assert calls.count("systemd-inhibit --list --no-pager --no-legend") == 10
    [failure] = _fails(calls)
    assert "no handle-power-key inhibitor is held" in failure


@needs_bash
def test_tc1327_knob_off_disables_and_stops_the_guard(tmp_path):
    done, calls = run_block(tmp_path, knob="false")
    _ok(done)
    assert _actions(calls) == ["systemctl daemon-reload",
                               "systemctl disable --now wall-power-button.service"]
    assert "guard off; one press shuts the panel down" in done.stdout


@needs_bash
def test_tc1327_an_invalid_knob_is_a_failed_step_and_starts_nothing(tmp_path):
    done, calls = run_block(tmp_path, knob="yes")
    _ok(done)
    assert [c for c in _actions(calls) if "enable" in c or "restart" in c] == []
    assert any("must be exactly true or false" in c for c in _fails(calls))


@needs_bash
@pytest.mark.parametrize("failing", ["wall-power-button.py", "wall-power-button.service"])
def test_tc1327_a_failing_install_is_reported_and_later_steps_still_run(tmp_path, failing):
    done, calls = run_block(tmp_path, fail_install=failing)
    _ok(done)
    assert any(("could not install " + failing) in c for c in _fails(calls))
    assert "enable_unit_now wall-power-button.service" in calls


@needs_bash
def test_tc1327_a_failing_enable_skips_the_inhibitor_check_and_the_block_finishes(tmp_path):
    done, calls = run_block(tmp_path, enable_status=1)
    _ok(done)
    assert not any(c.startswith("systemd-inhibit") for c in calls)
    assert any("enable --now FAILED" in c for c in _fails(calls))


@needs_bash
def test_tc1327_a_failing_restart_is_a_failed_step(tmp_path):
    done, calls = run_block(tmp_path, restart_status=1)
    _ok(done)
    assert any("did not restart onto its new definition" in c for c in _fails(calls))


# ── TC-1329: the alert route, installed and executed (Sol review 2, F3) ──────
#
# The route is three files on the panel: panel-alert (the daemon's ALERT_HOOK),
# homehub-alert.sh beside it, and /etc/wall-panel/panel-alert.env rendered by
# the firstboot block. These tests build that layout in a fake root FROM THE
# REAL ARTEFACTS (the payload files under their installed names, the env file
# written by the real firstboot block) and drive it from the daemon's own Shell,
# with a fake curl that records what it was asked to POST.

needs_posix_bash = pytest.mark.skipif(BASH is None or os.name != "posix",
                                      reason="runs the installed route: needs bash on a POSIX host")
TEST_HUB = "192.0.2.10"            # RFC 5737 documentation address, not a real hub
ROUTE_INSTALLS = [
    "install -d -m 0755 /usr/local/lib/wall-panel",
    "install -m 0755 {p}/homehub-alert.sh /usr/local/lib/wall-panel/homehub-alert.sh",
    "install -m 0755 {p}/panel-alert.sh /usr/local/lib/wall-panel/panel-alert",
]


def route_block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN panel-alert-route\n(.*?)# END panel-alert-route\n", text, re.S)
    assert found, "wall-firstboot.sh lost its panel-alert-route block"
    return found.group(1)


def run_route_block(tmp_path, env, *, fail_install=""):
    """The block, cut out and run in bash under set -euo pipefail. install and
    fail_step record; the env file is redirected into tmp_path (the one path
    rewritten), so what it writes is the block's own output."""
    calls = tmp_path / "route-calls"
    target = tmp_path / "etc" / "panel-alert.env"
    target.parent.mkdir(parents=True, exist_ok=True)
    rec = 'printf "%s\\n" "{kind} $*" >> "' + calls.as_posix() + '"'
    body = route_block()
    assert body.count("/etc/wall-panel/panel-alert.env") == 1
    body = body.replace("/etc/wall-panel/panel-alert.env", target.as_posix())
    assigns = ["{}='{}'".format(k, v) for k, v in env.items()]
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD="{}"'.format(WALL.as_posix()),
        *assigns,
        'log() { echo "[wall-firstboot] $*"; }',
        'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }',
        'fail_step() { echo "[wall-firstboot] ERROR: $*" >&2; %s; }' % rec.format(kind="fail_step"),
        'install() { %s; case "$*" in *"%s"*) [ -n "%s" ] && return 1 ;; esac; return 0; }'
        % (rec.format(kind="install"), fail_install or "\x01", fail_install),
        body,
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines, target


def installed_route(tmp_path, *, curl_exit=0, env_file=True):
    """Fake root: lib/panel-alert + lib/homehub-alert.sh (the payload files under
    their installed names), the env file the real block renders, a recording curl."""
    lib = tmp_path / "usr" / "local" / "lib" / "wall-panel"
    lib.mkdir(parents=True)
    for source, name in (("panel-alert.sh", "panel-alert"), ("homehub-alert.sh", "homehub-alert.sh")):
        shutil.copyfile(WALL / source, lib / name)
        (lib / name).chmod(0o755)
    env_path = tmp_path / "missing.env"
    if env_file:
        done, _calls, env_path = run_route_block(tmp_path, {"HUB_IP": TEST_HUB})
        _ok(done)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "curl-argv"
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '"
                    + record.as_posix() + "'\nexit " + str(curl_exit) + "\n", encoding="utf-8")
    curl.chmod(0o755)
    env = {"PATH": bindir.as_posix() + os.pathsep + os.environ.get("PATH", ""),
           "PANEL_ALERT_ENV_FILE": env_path.as_posix(),
           "PANEL_ALERT_STATE_DIR": (tmp_path / "state").as_posix()}
    return lib / "panel-alert", env, record


class RouteShell(pb.Shell):
    """The daemon's Shell with the REAL subprocess.run, the installed hook, a
    journal recorder and synchronous spawns; poweroff is never really run."""

    def __init__(self, m, hook, env):
        self.lines, self.commands = [], []

        def run(argv, **kwargs):
            self.commands.append(tuple(argv))
            if tuple(argv) == pb.POWEROFF_COMMAND:
                return subprocess.CompletedProcess(argv, 0)
            if tuple(argv) == pb.WAKE_COMMAND:
                return subprocess.CompletedProcess(argv, 0)
            done = subprocess.run(argv, env=env, capture_output=True, text=True, **kwargs)
            self.lines.extend((line, "route") for line in done.stdout.splitlines())
            return done

        super().__init__(m, run=run, write=lambda _doc: None,
                         log=lambda line, priority=6: self.lines.append((line, priority)),
                         alert_hook=hook, spawn=lambda target: target())


def test_tc1329_the_panels_alert_helper_is_the_hubs_byte_for_byte():
    shipped = (WALL / "homehub-alert.sh").read_bytes()
    assert shipped == (ROOT / "stack" / "monitoring" / "homehub-alert.sh").read_bytes(), \
        "stack/autoinstall/wall/homehub-alert.sh must be a byte-identical copy of the hub's"


@needs_posix_bash
def test_tc1329_a_kiosk_unavailable_alert_is_posted_to_the_hubs_ntfy(tmp_path):
    hook, env, record = installed_route(tmp_path)
    m = machine()
    shell = RouteShell(m, hook, env)
    shell.execute(press(m, 10.0), 10.0)
    shell.execute(m.tick(12.0, no_ack), 12.0)         # no acknowledgement: the alert
    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[-1] == "http://%s:8090/homehub-alerts" % TEST_HUB
    assert "Title: Wall panel: power-button-kiosk-unavailable" in argv
    assert "Priority: high" in argv
    body = argv[argv.index("--data-binary") + 1]
    assert body.startswith("the kiosk did not show power-button warning 101")
    assert (tmp_path / "state" / "alerted-wall-panel.power-button-kiosk-unavailable").exists()
    assert any("alert delivered to http://%s:8090/homehub-alerts [wall-panel.power-button-kiosk-unavailable]"
               % TEST_HUB in line for line, _ in shell.lines), shell.lines
    assert not any("NOT DELIVERED" in line for line, _ in shell.lines)
    # A second occurrence inside 30 min is journaled but not re-sent.
    record.unlink()
    shell.alert("power-button-kiosk-unavailable", "again")
    assert not record.exists()
    assert any("not re-sent" in line for line, _ in shell.lines)


@needs_posix_bash
@pytest.mark.parametrize("curl_exit,env_file,route_says", [
    (7, True, "ALERT NOT DELIVERED to http://%s:8090/homehub-alerts" % TEST_HUB),
    (0, False, "ALERT NOT SENT (no usable NTFY_BIND_IP/LAN_IP"),
])
def test_tc1329_an_unreachable_or_unconfigured_hub_is_journaled_and_changes_nothing(
        tmp_path, curl_exit, env_file, route_says):
    hook, env, _record = installed_route(tmp_path, curl_exit=curl_exit, env_file=env_file)
    m = machine()
    shell = RouteShell(m, hook, env)
    shell.execute(press(m, 10.0), 10.0)
    shell.execute(m.tick(12.0, no_ack), 12.0)
    assert any(route_says in line for line, _ in shell.lines), shell.lines
    assert any(line.startswith("ALERT NOT DELIVERED power-button-kiosk-unavailable: the alert "
                               "route could not reach the hub (exit 4)") for line, _ in shell.lines)
    assert m.state == pb.ARMED
    shell.execute(press(m, 13.0), 13.0)                # the blind second press
    assert shell.commands[-1] == pb.POWEROFF_COMMAND and m.state == pb.SHUTTING_DOWN


def test_tc1329_a_hung_alert_route_never_delays_the_second_press(tmp_path):
    # Real threads (the daemon's own spawn) this time: the route blocks until
    # released, and the second press must reach systemctl poweroff while the
    # alert is still blocked.
    import threading
    hook = tmp_path / "panel-alert"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    if not os.access(hook, os.X_OK):
        pytest.skip("this filesystem cannot mark a file executable")
    release, entered, commands = threading.Event(), threading.Event(), []

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        if argv[0] == str(hook):
            entered.set()
            release.wait(10)
        return subprocess.CompletedProcess(argv, 0)

    m = machine()
    shell = pb.Shell(m, run=run, write=lambda _doc: None, log=lambda *a: None, alert_hook=hook)
    pressed = threading.Event()

    def the_loop():                     # what run() does: the ack tick, then a press
        shell.execute(m.tick(12.0, no_ack), 12.0)
        shell.execute(press(m, 13.0), 13.0)
        pressed.set()

    try:
        shell.execute(press(m, 10.0), 10.0)
        threading.Thread(target=the_loop, daemon=True).start()
        assert pressed.wait(3), "the second press waited for the alert route"
        assert entered.wait(3), "the alert never started"
        assert pb.POWEROFF_COMMAND in commands and not release.is_set()
    finally:
        release.set()


@needs_bash
def test_tc1329_firstboot_renders_the_route_from_wall_env(tmp_path):
    done, calls, target = run_route_block(tmp_path, {"HUB_IP": TEST_HUB, "WALL_ALERT_NTFY_PORT": "8091",
                                                     "WALL_ALERT_TOPIC": "panel-test"})
    _ok(done)
    p = WALL.as_posix()
    assert [c for c in calls if c.startswith("install")] == [i.format(p=p) for i in ROUTE_INSTALLS]
    assert _fails(calls) == []
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [l for l in lines if not l.startswith("#")] == [
        "LAN_IP=" + TEST_HUB, "NTFY_PORT=8091", "HOMEHUB_ALERT_TOPIC=panel-test"]
    assert "panel alerts go to the hub's ntfy (port 8091, topic panel-test)" in done.stdout


@needs_bash
def test_tc1329_firstboot_defaults_port_and_topic_to_the_hubs(tmp_path):
    done, _calls, target = run_route_block(tmp_path, {"HUB_IP": TEST_HUB})
    _ok(done)
    assert "NTFY_PORT=8090" in target.read_text(encoding="utf-8").splitlines()
    assert "HOMEHUB_ALERT_TOPIC=homehub-alerts" in target.read_text(encoding="utf-8").splitlines()
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    for line in ("HUB_IP=REPLACE_WITH_HUB_IP", "WALL_ALERT_NTFY_PORT=8090",
                 "WALL_ALERT_TOPIC=homehub-alerts"):
        assert re.search("^" + re.escape(line) + "$", env, re.M), line
    hub_env = (ROOT / "stack" / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^NTFY_PORT=8090$", hub_env, re.M)
    assert re.search(r"^HOMEHUB_ALERT_TOPIC=homehub-alerts$", hub_env, re.M)


@needs_bash
@pytest.mark.parametrize("env,failed", [
    ({"HUB_IP": ""}, False),
    ({}, False),
    ({"HUB_IP": "homehub.lan"}, True),
    ({"HUB_IP": TEST_HUB, "WALL_ALERT_NTFY_PORT": "80;x"}, True),
    ({"HUB_IP": TEST_HUB, "WALL_ALERT_TOPIC": "a/b"}, True),
])
def test_tc1329_no_or_bad_hub_address_leaves_the_route_journal_only(tmp_path, env, failed):
    target = tmp_path / "etc" / "panel-alert.env"
    target.parent.mkdir(parents=True)
    target.write_text("LAN_IP=198.51.100.1\n", encoding="utf-8")   # a stale earlier render
    done, calls, target = run_route_block(tmp_path, env)
    _ok(done)
    assert not target.exists(), "a stale route must not outlive its knob"
    if failed:
        assert any("is malformed; panel alerts are journal-only" in c for c in _fails(calls))
    else:
        assert _fails(calls) == []
        assert "HUB_IP is not set in wall.env; panel alerts are journal-only" in done.stderr


@needs_bash
@pytest.mark.parametrize("failing", ["homehub-alert.sh", "panel-alert.sh"])
def test_tc1329_a_failing_route_install_is_a_failed_step_and_the_block_finishes(tmp_path, failing):
    done, calls, _target = run_route_block(tmp_path, {"HUB_IP": TEST_HUB}, fail_install=failing)
    _ok(done)
    assert any("could not install" in c and "journal-only" in c for c in _fails(calls))
