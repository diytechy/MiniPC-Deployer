"""SR-108/SR-109 (OfficeWallNaglight WSN-097): the wall panel records a
meeting's audio, both directions, only while the kiosk confirms the indicator.

Layers, each against the real artefact:

* TC-1400 -- the pure RecordingMachine: every transition the plan names
  (manual, call, manual-during-call, a stop during a call suppressing the
  automatic trigger until that call ends, repeated call notifications, a
  failed capture, a lost indicator).
* TC-1401 -- the loop (Recorder.tick) with an injected clock and a fake
  capture: no indicator no capture, a beat for an older recording does not
  count, capture stops within 2 s of the last beat, a call resumes into a new
  file when the indicator comes back, a manual start without an indicator
  gives up.
* TC-1402 -- a capture that crashes mid-recording, and a daemon that crashed:
  the partial file is finalised with its hash, the state returns to idle and
  the call is not re-triggered in a loop.
* TC-1403 -- the bounds: the spool's cap and the disk floor refuse a start and
  say which; a capture is started with -fs set to the room left and one that
  stops on it is "spool-full"; both budgets full is visible in the document.
* TC-1404 -- the inputs: call-state, request and acknowledgement parsing.
* TC-1405 -- the state document, its atomic write, and a bounded stop.
* TC-1406 -- TONES through the real ffmpeg (skipped without ffmpeg/libopus):
  the near input lands on the left channel and the far input on the right,
  and the Recorder end to end with tone inputs records, stops on a lost
  indicator within 2 s and finalises a decodable file; -fs stops on its limit.
* TC-1407 -- the shipped unit, knobs and files.
* TC-1408 -- the firstboot block, cut out between its markers and EXECUTED
  under bash with every external command a recording function.

WHAT THIS DOES NOT PROVE. Nothing here opens the panel's ALSA PCMs. That
`mic_selected` and `bus_monitor` can be read at the same time as the call legs
and at what cost was measured on the panel on 2026-10-22 (HomeHub FOLLOWUPS,
W6 feasibility); a test call recorded end to end with both directions audible
is the coordinator's live step.

Implements tests: TC-1400, TC-1401, TC-1402, TC-1403, TC-1404, TC-1405,
TC-1406, TC-1407, TC-1408.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
BASH = shutil.which("bash")
FFMPEG = shutil.which("ffmpeg")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _load():
    spec = importlib.util.spec_from_file_location("wall_meeting_recorder", WALL / "wall-meeting-recorder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mr = _load()


def _has_libopus():
    if FFMPEG is None:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libopus" in out


needs_ffmpeg = pytest.mark.skipif(not _has_libopus(), reason="needs ffmpeg with libopus")


# ── TC-1400: the machine ─────────────────────────────────────────────────────

def test_tc1400_manual_start_and_stop():
    m = mr.RecordingMachine(seq=10)
    assert m.manual_start() and m.intent == mr.MANUAL and m.seq == 11
    assert not m.manual_start(), "a repeated start is nothing"
    assert m.manual_stop() and m.intent == mr.IDLE
    assert not m.manual_stop()


def test_tc1400_a_call_from_a_listed_gateway_starts_and_its_end_stops():
    m = mr.RecordingMachine(seq=0)
    assert m.call_up("t1", "desktop-office", auto=True) and m.intent == mr.CALL and m.seq == 1
    assert m.call_down() and m.intent == mr.IDLE


def test_tc1400_a_call_from_another_gateway_or_with_no_list_does_not_start():
    m = mr.RecordingMachine()
    assert not m.call_up("t1", "pixel-10-pro", auto=False) and m.intent == mr.IDLE
    m = mr.RecordingMachine(auto_call=False)
    assert not m.call_up("t1", "desktop-office", auto=True) and m.intent == mr.IDLE


def test_tc1400_repeated_call_notifications_are_idempotent():
    m = mr.RecordingMachine(seq=5)
    m.call_up("t1", "desktop-office")
    for _ in range(5):
        assert not m.call_up("t1", "desktop-office")
    assert m.intent == mr.CALL and m.seq == 6


def test_tc1400_a_manual_start_during_a_call_joins_the_same_recording():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    seq = m.seq
    assert m.manual_start() and m.intent == mr.MANUAL_CALL
    assert m.seq == seq, "same recording: the indicator's sequence does not move"


def test_tc1400_a_call_arriving_during_a_manual_recording_joins_it():
    m = mr.RecordingMachine(seq=0)
    m.manual_start()
    seq = m.seq
    assert m.call_up("t1", "desktop-office") and m.intent == mr.MANUAL_CALL and m.seq == seq
    assert m.call_down() and m.intent == mr.MANUAL, "the person started it; the call ending does not stop it"


def test_tc1400_a_manual_stop_during_a_call_suppresses_it_until_that_call_ends():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    assert m.manual_stop() and m.intent == mr.IDLE
    for _ in range(3):
        assert not m.call_up("t1", "desktop-office") and m.intent == mr.IDLE
    m.call_down()
    assert m.call_up("t2", "desktop-office") and m.intent == mr.CALL, "the next call records again"


def test_tc1400_a_manual_stop_of_a_joined_recording_also_suppresses_the_call():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    m.manual_start()
    assert m.manual_stop() and m.intent == mr.IDLE
    assert not m.call_up("t1", "desktop-office")


def test_tc1400_a_different_call_key_is_a_new_call_even_without_an_idle_between():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    m.manual_stop()
    assert m.call_up("t2", "desktop-office") and m.intent == mr.CALL


def test_tc1400_a_manual_start_after_a_suppressing_stop_records_again():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    m.manual_stop()
    assert m.manual_start() and m.intent == mr.MANUAL


def test_tc1400_a_failed_capture_returns_to_idle_and_does_not_retrigger_the_call():
    m = mr.RecordingMachine(seq=0)
    m.call_up("t1", "desktop-office")
    assert m.capture_failed() and m.intent == mr.IDLE
    assert not m.call_up("t1", "desktop-office")
    m.call_down()
    assert m.call_up("t2", "desktop-office")


def test_tc1400_a_lost_indicator_ends_a_manual_recording_and_keeps_a_calls_intent():
    m = mr.RecordingMachine(seq=0)
    m.manual_start()
    assert m.indicator_lost() and m.intent == mr.IDLE
    m.call_up("t1", "desktop-office")
    assert not m.indicator_lost() and m.intent == mr.CALL
    m.manual_start()
    assert m.indicator_lost() and m.intent == mr.CALL


# ── the rig for TC-1401..TC-1405 ─────────────────────────────────────────────

class FakeProcess:
    """A capture child: writes `size` bytes into its output at start."""

    def __init__(self, argv, rig):
        self.argv = argv
        self.rig = rig
        self.returncode = None
        self.signals = []
        out = Path(argv[-1])
        out.write_bytes(b"O" * rig.capture_bytes)
        rig.started.append(out)

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if not self.rig.ignore_sigint:
            self.returncode = 255

    def kill(self):
        self.signals.append("KILL")
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Rig:
    def __init__(self, tmp_path, *, gateways="desktop-office", spool_mb=512, floor_mb=256, free=None):
        self.tmp = tmp_path
        self.now = 1000.0
        self.wall_offset = 1_792_000_000.0
        self.capture_bytes = 50_000
        self.ignore_sigint = False
        self.started = []
        self.processes = []
        self.argvs = []
        self.alerts = []
        self.renderer = tmp_path / "renderer"
        self.renderer.mkdir()
        (tmp_path / "audio-mode").write_text("bus\n")
        (tmp_path / "mic.env").write_text("WALL_AUDIO_MIC_SOURCE=mic_clean\n")
        (tmp_path / "mute.env").write_text("WALL_AUDIO_INPUT_MUTED=0\n")
        self.call_file = tmp_path / "call-state.json"
        self.spool = mr.Spool(tmp_path / "spool")
        if free is not None:
            self.spool.free_bytes = lambda: free
        settings = mr.settings_from({"WALL_MEETING_SPOOL_MAX_MB": str(spool_mb),
                                     "WALL_MEETING_MIN_FREE_MB": str(floor_mb),
                                     "WALL_MEETING_CALL_GATEWAYS": gateways})
        rig = self

        def popen(argv, **kwargs):
            proc = FakeProcess(argv, rig)
            rig.processes.append(proc)
            rig.argvs.append(argv)
            return proc

        self.capture = mr.Capture(popen=popen, clock=lambda: rig.now, sleep=self._sleep)
        self.rec = mr.Recorder(
            settings, spool=self.spool, state_file=tmp_path / "state" / "state.json",
            request_file=self.renderer / "meeting-request.json",
            ack_file=self.renderer / "meeting-indicator-ack.json",
            call_file=self.call_file, mode_file=tmp_path / "audio-mode",
            mic_env=tmp_path / "mic.env", mute_env=tmp_path / "mute.env",
            capture=self.capture, clock=lambda: rig.now, wall=lambda: rig.wall_offset + rig.now,
            alert_fn=lambda key, message: rig.alerts.append(key))
        self.rec.prime()
        self.request_id = 0
        self.beat = 0

    def _sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds

    def request(self, action):
        self.request_id += 1
        (self.renderer / "meeting-request.json").write_text(json.dumps(
            {"schemaVersion": 1, "id": self.request_id, "action": action,
             "requestedAt": int((self.wall_offset + self.now) * 1000)}))

    def ack(self, seq=None):
        self.beat += 1
        (self.renderer / "meeting-indicator-ack.json").write_text(json.dumps(
            {"schemaVersion": 1, "seq": self.rec.machine.seq if seq is None else seq, "beat": self.beat,
             "shownAt": int((self.wall_offset + self.now) * 1000)}))

    def call(self, state="call", since="2026-10-22T16:07:52Z", gateway="desktop-office"):
        self.call_file.write_text(json.dumps({"state": state, "since": since, "gateway": gateway,
                                              "headset": None, "mic": None, "revision": 2}))
        t = self.wall_offset + self.now
        os.utime(self.call_file, (t, t))

    def hang_up(self):
        self.call_file.write_text(json.dumps({"state": "idle", "since": None, "gateway": None}))

    def tick(self):
        return self.rec.tick()

    def run_beating(self, seconds, step=0.25, beat_every=0.5):
        """Tick for `seconds` with the kiosk beating every `beat_every`."""
        doc, waited, since_beat = None, 0.0, beat_every
        while waited < seconds:
            if since_beat >= beat_every:
                self.ack()
                since_beat = 0.0
            if self.call_file.exists():
                t = self.wall_offset + self.now
                os.utime(self.call_file, (t, t))
            doc = self.tick()
            self.advance(step)
            waited += step
            since_beat += step
        return doc

    def ready(self):
        return sorted(p.name for p in self.spool.ready.iterdir())


# ── TC-1401: no indicator, no capture ────────────────────────────────────────

def test_tc1401_no_acknowledgement_means_no_capture(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    for _ in range(8):
        doc = rig.tick()
        rig.advance(0.25)
    assert rig.processes == []
    assert doc["indicator"] is True and doc["phase"] == "waiting", "the indicator is asked for first"


def test_tc1401_an_acknowledgement_for_an_older_recording_does_not_count(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    rig.tick()
    rig.ack(seq=rig.rec.machine.seq - 1)
    rig.tick()
    assert rig.processes == []


def test_tc1401_a_fresh_beat_starts_the_capture(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    rig.tick()
    rig.ack()
    doc = rig.tick()
    assert len(rig.processes) == 1 and doc["phase"] == "recording" and doc["startedAt"] is not None


def test_tc1401_capture_stops_within_2_s_of_the_last_beat(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    rig.run_beating(3.0)
    assert len(rig.processes) == 1
    # The rig writes each beat and ticks at the same instant, so the daemon's
    # observation time is the kiosk's write time.
    beat_at = rig.rec.beat_at
    stopped_at = None
    for _ in range(40):
        rig.tick()
        if rig.processes[0].returncode is not None:
            stopped_at = rig.now
            break
        rig.advance(0.25)
    assert stopped_at is not None, "never stopped"
    assert stopped_at - beat_at <= 2.0, (stopped_at, beat_at)
    assert rig.processes[0].signals[0] == mr.signal.SIGINT
    assert rig.ready() == [rig.started[0].name, rig.started[0].name + ".sha256"]
    doc = rig.tick()
    assert doc["intent"] == "idle" and doc["refusal"]["reason"] == "indicator-lost"


def test_tc1401_a_call_resumes_into_a_new_file_when_the_indicator_returns(tmp_path):
    rig = Rig(tmp_path)
    rig.call()
    rig.run_beating(2.0)
    assert len(rig.processes) == 1
    for _ in range(12):  # the kiosk stops beating for 3 s (a renderer restart)
        rig.call()
        rig.tick()
        rig.advance(0.25)
    assert rig.processes[0].returncode is not None
    assert rig.rec.machine.intent == mr.CALL, "the call's intent survives"
    rig.advance(1.0)
    rig.run_beating(2.0)
    assert len(rig.processes) == 2 and rig.started[0] != rig.started[1]


def test_tc1401_a_manual_start_with_no_indicator_gives_up_after_10_s(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    doc = None
    for _ in range(int(11 / 0.25)):
        doc = rig.tick()
        rig.advance(0.25)
    assert rig.processes == []
    assert doc["intent"] == "idle" and doc["refusal"]["reason"] == "indicator-unavailable"


def test_tc1401_a_call_without_an_indicator_waits_and_never_captures(tmp_path):
    rig = Rig(tmp_path)
    for _ in range(80):
        rig.call()
        doc = rig.tick()
        rig.advance(0.25)
    assert rig.processes == [] and doc["intent"] == "call" and doc["phase"] == "waiting"


def test_tc1401_the_call_ending_stops_and_finalises(tmp_path):
    rig = Rig(tmp_path)
    rig.call()
    rig.run_beating(2.0)
    rig.hang_up()
    doc = rig.tick()
    assert rig.processes[0].returncode is not None and doc["intent"] == "idle"
    assert len(rig.ready()) == 2


def test_tc1401_a_manual_stop_during_a_call_stops_and_the_call_does_not_restart_it(tmp_path):
    rig = Rig(tmp_path)
    rig.call()
    rig.run_beating(2.0)
    rig.request("stop")
    rig.run_beating(3.0)
    assert len(rig.processes) == 1 and rig.processes[0].returncode is not None
    assert rig.rec.machine.intent == mr.IDLE


def test_tc1401_a_manual_start_during_a_call_keeps_one_file(tmp_path):
    rig = Rig(tmp_path)
    rig.call()
    rig.run_beating(2.0)
    rig.request("start")
    doc = rig.run_beating(2.0)
    assert len(rig.processes) == 1 and doc["intent"] == "manual-call"
    rig.hang_up()
    doc = rig.run_beating(1.0)
    assert len(rig.processes) == 1 and rig.processes[0].returncode is None and doc["intent"] == "manual"


def test_tc1401_an_unlisted_gateways_call_is_not_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.call(gateway="pixel-10-pro")
    rig.run_beating(2.0)
    assert rig.processes == []


# ── TC-1402: crashes ─────────────────────────────────────────────────────────

def test_tc1402_a_capture_that_crashes_is_finalised_and_returns_to_idle(tmp_path):
    rig = Rig(tmp_path)
    rig.call()
    rig.run_beating(2.0)
    rig.processes[0].returncode = 1
    doc = rig.run_beating(1.0)
    assert doc["intent"] == "idle" and doc["refusal"]["reason"] == "recorder-failed"
    assert rig.ready() == [rig.started[0].name, rig.started[0].name + ".sha256"]
    sidecar = (rig.spool.ready / (rig.started[0].name + ".sha256")).read_text().split()[0]
    assert sidecar == mr.sha256_of(rig.spool.ready / rig.started[0].name)
    rig.run_beating(5.0)
    assert len(rig.processes) == 1, "the same call is not retried in a loop"
    assert "capture-failed" in rig.alerts


def test_tc1402_a_daemon_crash_leaves_a_partial_that_the_next_start_finalises(tmp_path):
    spool = mr.Spool(tmp_path / "spool")
    spool.ensure()
    partial = spool.recording / "meeting-20261022T160752Z.opus"
    partial.write_bytes(b"x" * 4096)
    (spool.ready / "meeting-20261022T150000Z.opus").write_bytes(b"y" * 4096)
    (spool.recording / "meeting-20261022T170000Z.opus").write_bytes(b"z" * 10)
    done = spool.recover()
    names = sorted(os.listdir(spool.ready))
    assert names == ["meeting-20261022T150000Z.opus", "meeting-20261022T150000Z.opus.sha256",
                     "meeting-20261022T160752Z.opus", "meeting-20261022T160752Z.opus.sha256"]
    assert sorted(done) == ["meeting-20261022T150000Z.opus", "meeting-20261022T160752Z.opus"]
    assert os.listdir(spool.recording) == [], "a file with no audio is discarded"


def test_tc1402_the_service_stopping_finalises_the_recording(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    rig.run_beating(2.0)
    rig.rec.shutdown()
    assert rig.processes[0].signals[0] == mr.signal.SIGINT and len(rig.ready()) == 2


# ── TC-1403: bounds ──────────────────────────────────────────────────────────

MIB = 1024 * 1024


@pytest.mark.parametrize("used,cap,free,floor,room,reason", [
    (0, 512 * MIB, 80_000 * MIB, 5120 * MIB, 512 * MIB, None),
    (510 * MIB, 512 * MIB, 80_000 * MIB, 5120 * MIB, 2 * MIB, "spool-full"),
    (0, 512 * MIB, 5122 * MIB, 5120 * MIB, 2 * MIB, "disk-low"),
    (0, 512 * MIB, 100 * MIB, 5120 * MIB, 0, "disk-low"),
    (600 * MIB, 512 * MIB, 80_000 * MIB, 5120 * MIB, 0, "spool-full"),
])
def test_tc1403_spool_room(used, cap, free, floor, room, reason):
    assert mr.spool_room(used, cap, free, floor) == (room, reason)


def test_tc1403_a_full_spool_refuses_a_manual_start_and_says_so(tmp_path):
    rig = Rig(tmp_path, spool_mb=16)
    rig.spool.ensure()
    (rig.spool.ready / "meeting-20261022T150000Z.opus").write_bytes(b"x" * (13 * MIB))
    rig.request("start")
    doc = rig.run_beating(1.0)
    assert rig.processes == [] and doc["intent"] == "idle"
    assert doc["refusal"]["reason"] == "spool-full" and doc["indicator"] is False


def test_tc1403_a_low_disk_refuses_too(tmp_path):
    rig = Rig(tmp_path, floor_mb=256, free=257 * MIB)
    rig.request("start")
    doc = rig.run_beating(1.0)
    assert rig.processes == [] and doc["refusal"]["reason"] == "disk-low"


def test_tc1403_the_capture_is_bounded_by_the_room_left(tmp_path):
    rig = Rig(tmp_path, spool_mb=16)
    rig.spool.ensure()
    (rig.spool.ready / "meeting-20261022T150000Z.opus").write_bytes(b"x" * (2 * MIB))
    rig.request("start")
    rig.run_beating(1.0)
    argv = rig.argvs[0]
    assert argv[argv.index("-fs") + 1] == str(14 * MIB - mr.FS_MARGIN)
    assert argv[argv.index("-t") + 1] == str(240 * 60)


def test_tc1403_a_call_blocked_by_a_full_spool_starts_once_the_hub_drains_it(tmp_path):
    rig = Rig(tmp_path, spool_mb=16)
    rig.spool.ensure()
    full = rig.spool.ready / "meeting-20261022T150000Z.opus"
    full.write_bytes(b"x" * (13 * MIB))
    rig.call()
    doc = rig.run_beating(2.0)
    assert rig.processes == [] and doc["phase"] == "blocked" and doc["indicator"] is False
    assert doc["refusal"]["reason"] == "spool-full"
    full.unlink()
    doc = rig.run_beating(1.0)
    assert len(rig.processes) == 1 and doc["phase"] == "recording"


def test_tc1403_a_capture_that_stops_on_its_size_limit_is_spool_full(tmp_path):
    rig = Rig(tmp_path, spool_mb=16)
    rig.capture_bytes = 16 * MIB
    rig.call()
    rig.run_beating(1.0)
    rig.processes[0].returncode = 0
    doc = rig.run_beating(1.0)
    assert doc["phase"] == "blocked" and doc["refusal"]["reason"] == "spool-full"
    assert len(rig.processes) == 1


def test_tc1403_both_budgets_full_is_on_the_document(tmp_path):
    rig = Rig(tmp_path, spool_mb=16)
    rig.spool.ensure()
    (rig.spool.ready / "meeting-20261022T150000Z.opus").write_bytes(b"x" * (13 * MIB))
    (rig.spool.root / "hub-state.json").write_text(json.dumps({"schemaVersion": 1, "quota": "full"}))
    t = rig.wall_offset + rig.now
    os.utime(rig.spool.root / "hub-state.json", (t, t))
    rig.request("start")
    doc = rig.run_beating(1.0)
    assert doc["refusal"]["reason"] == "spool-full" and doc["hub"] == "full"


def test_tc1403_a_stale_hub_report_is_unknown(tmp_path):
    rig = Rig(tmp_path)
    rig.spool.ensure()
    path = rig.spool.root / "hub-state.json"
    path.write_text(json.dumps({"schemaVersion": 1, "quota": "full"}))
    t = rig.wall_offset + rig.now - 2000
    os.utime(path, (t, t))
    assert rig.tick()["hub"] == "unknown"


def test_tc1403_not_bus_mode_refuses(tmp_path):
    rig = Rig(tmp_path)
    (tmp_path / "audio-mode").write_text("trigger\n")
    rig.request("start")
    doc = rig.run_beating(1.0)
    assert rig.processes == [] and doc["available"] is False and doc["refusal"]["reason"] == "unavailable"


# ── TC-1404: inputs ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,age,expected", [
    ('{"state":"call","since":"S","gateway":"desktop-office"}', 1, ("S", "desktop-office")),
    ('{"state":"bridge","since":"S","gateway":"desktop-office"}', 1, ("S", "desktop-office")),
    ('{"state":"call","since":"S","gateway":"desktop-office"}', 31, (None, None)),
    ('{"state":"idle","since":null,"gateway":null}', 1, (None, None)),
    ('{"state":"call","since":"S","gateway":"5C:F3:70:00:00:00"}', 1, ("S", None)),
    ('{"state":"call","gateway":"desktop-office"}', 1, ("unknown", "desktop-office")),
    ("not json", 1, (None, None)),
    (None, None, (None, None)),
])
def test_tc1404_call_state(text, age, expected):
    assert mr.parse_call_state(text, age) == expected


@pytest.mark.parametrize("text,expected", [
    ('{"schemaVersion":1,"id":3,"action":"start","requestedAt":5}', (3, "start", 5.0)),
    ('{"schemaVersion":1,"id":3,"action":"stop","requestedAt":5}', (3, "stop", 5.0)),
    ('{"schemaVersion":1,"id":true,"action":"start","requestedAt":5}', None),
    ('{"schemaVersion":1,"id":3,"action":"pause","requestedAt":5}', None),
    ('{"schemaVersion":2,"id":3,"action":"start","requestedAt":5}', None),
    ("x" * 5000, None),
])
def test_tc1404_request(text, expected):
    assert mr.parse_request(text) == expected


@pytest.mark.parametrize("text,expected", [
    ('{"schemaVersion":1,"seq":7,"beat":2}', (7, 2)),
    ('{"schemaVersion":1,"seq":7,"beat":false}', None),
    ('{"schemaVersion":1,"seq":"7","beat":2}', None),
    ('[7,2]', None),
])
def test_tc1404_ack(text, expected):
    assert mr.parse_ack(text) == expected


def test_tc1404_a_request_from_before_a_restart_is_not_replayed(tmp_path):
    rig = Rig(tmp_path)
    rig.request("start")
    rig.rec.prime()  # a restart reads the request it finds and marks it seen
    rig.run_beating(1.0)
    assert rig.processes == []


def test_tc1404_an_old_request_is_ignored(tmp_path):
    rig = Rig(tmp_path)
    rig.request_id += 1
    (rig.renderer / "meeting-request.json").write_text(json.dumps(
        {"schemaVersion": 1, "id": rig.request_id, "action": "start",
         "requestedAt": int((rig.wall_offset + rig.now - 60) * 1000)}))
    rig.run_beating(1.0)
    assert rig.processes == []


def test_tc1404_a_symlinked_ack_is_refused(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"schemaVersion":1,"seq":1,"beat":1}')
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("no O_NOFOLLOW on this platform")
    assert mr.read_small(link) is None


@pytest.mark.parametrize("env,key,value", [
    ({"WALL_MEETING_SPOOL_MAX_MB": "8"}, "WALL_MEETING_SPOOL_MAX_MB", 512),
    ({"WALL_MEETING_SPOOL_MAX_MB": "1024"}, "WALL_MEETING_SPOOL_MAX_MB", 1024),
    ({"WALL_MEETING_BITRATE_KBPS": "abc"}, "WALL_MEETING_BITRATE_KBPS", 48),
    ({"WALL_MEETING_CALL_GATEWAYS": "desktop-office, BAD NAME,pixel-10-pro"},
     "WALL_MEETING_CALL_GATEWAYS", ["desktop-office", "pixel-10-pro"]),
])
def test_tc1404_settings_are_bounded(env, key, value):
    assert mr.settings_from(env)[key] == value


def test_tc1404_the_mic_source_and_mute_reach_the_capture_and_the_document(tmp_path):
    rig = Rig(tmp_path)
    (tmp_path / "mute.env").write_text("WALL_AUDIO_INPUT_MUTED=1\n")
    seen = {}

    def popen(argv, env=None, **kwargs):
        seen["env"] = env
        proc = FakeProcess(argv, rig)
        rig.processes.append(proc)
        return proc

    rig.capture.popen = popen
    rig.request("start")
    doc = rig.run_beating(1.0)
    assert seen["env"]["WALL_AUDIO_MIC_SOURCE"] == "mic_clean"
    assert doc["nearMuted"] is True


# ── TC-1405: the document and the stop ───────────────────────────────────────

def test_tc1405_the_state_document_carries_the_contract_fields(tmp_path):
    rig = Rig(tmp_path)
    doc = rig.tick()
    assert set(doc) == {"schemaVersion", "seq", "intent", "phase", "indicator", "startedAt", "gateway",
                        "nearMuted", "refusal", "spool", "hub", "available", "unavailableReason",
                        "autoCall", "observedAt"}
    assert doc["intent"] == "idle" and doc["indicator"] is False and doc["autoCall"] is True
    on_disk = json.loads((tmp_path / "state" / "state.json").read_text())
    assert on_disk["seq"] == doc["seq"]
    if os.name == "posix":
        assert (tmp_path / "state" / "state.json").stat().st_mode & 0o777 == 0o644


def test_tc1405_no_gateways_means_no_automatic_trigger(tmp_path):
    rig = Rig(tmp_path, gateways="")
    assert rig.tick()["autoCall"] is False
    rig.call()
    rig.run_beating(2.0)
    assert rig.processes == []


def test_tc1405_a_capture_that_ignores_sigint_is_killed_within_the_grace(tmp_path):
    rig = Rig(tmp_path)
    rig.ignore_sigint = True
    rig.request("start")
    rig.run_beating(1.0)
    before = rig.now
    rig.rec.shutdown()
    assert rig.processes[0].signals == [mr.signal.SIGINT, "KILL"]
    assert rig.now - before <= mr.STOP_GRACE_S + 0.05


def test_tc1405_the_timings_add_up_to_under_2_s():
    assert mr.ACK_FRESH_S + mr.POLL_S + mr.STOP_GRACE_S < 2.0


def test_tc1405_recording_names_are_utc_and_unique():
    taken = {"meeting-20261022T160752Z.opus"}
    t = 1792685272.0  # 2026-10-22T16:07:52Z
    assert mr.recording_name(t, set()) == "meeting-20261022T160752Z.opus"
    assert mr.recording_name(t, taken) == "meeting-20261022T160752Z-2.opus"
    assert mr.NAME.match(mr.recording_name(t, taken))


# ── TC-1406: tones through the real ffmpeg ───────────────────────────────────

def _band_rms(path, channel, freq):
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path), "-af",
         "pan=mono|c0=c%d,bandpass=f=%d:width_type=q:w=30,astats=metadata=0" % (channel, freq),
         "-f", "null", "-"], capture_output=True, text=True).stderr
    values = re.findall(r"RMS level dB: (-?[0-9.]+|-inf)", out)
    return float(values[-1]) if values and values[-1] != "-inf" else -200.0


TONE_NEAR = ["-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000"]
TONE_FAR = ["-f", "lavfi", "-i", "sine=frequency=2700:sample_rate=48000,aformat=channel_layouts=stereo"]


@needs_ffmpeg
def test_tc1406_near_lands_left_and_far_lands_right(tmp_path):
    out = tmp_path / "meeting-20261022T160752Z.opus"
    argv = mr.capture_argv(str(out), 10 * MIB, 3, 48, near=TONE_NEAR, far=TONE_FAR, ffmpeg=FFMPEG)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_name,channels", "-of", "compact", str(out)],
                           capture_output=True, text=True).stdout
    assert "codec_name=opus" in probe and "channels=2" in probe
    assert _band_rms(out, 0, 600) > _band_rms(out, 0, 2700) + 25
    assert _band_rms(out, 1, 2700) > _band_rms(out, 1, 600) + 25


@needs_ffmpeg
def test_tc1406_the_size_limit_stops_the_capture_cleanly(tmp_path):
    out = tmp_path / "meeting-20261022T160752Z.opus"
    argv = mr.capture_argv(str(out), 20_000, 60, 48, near=TONE_NEAR, far=TONE_FAR, ffmpeg=FFMPEG)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    size = out.stat().st_size
    assert 20_000 * mr.LIMIT_FRACTION <= size <= 20_000 + mr.FS_MARGIN, size


@needs_ffmpeg
def test_tc1406_the_recorder_end_to_end_with_tones(tmp_path):
    """Real ffmpeg, real time: the indicator beats for 2 s, then stops; the
    capture must stop within 2 s and the finished file must decode with both
    tones in their channels."""
    import time as _time
    rig = Rig(tmp_path)
    rig.rec.capture = mr.Capture()
    rig.rec.clock = _time.monotonic
    rig.rec.wall = _time.time
    rig.wall_offset, rig.now = 0.0, _time.time()

    def argv_for(output, limit, seconds, bitrate):
        live_near = ["-re"] + TONE_NEAR
        live_far = ["-re"] + TONE_FAR
        return mr.capture_argv(output, limit, seconds, bitrate, near=live_near, far=live_far, ffmpeg=FFMPEG)

    rig.rec.argv_for = argv_for
    rig.request("start")
    started = _time.monotonic()
    while _time.monotonic() - started < 2.5:
        rig.now = _time.time()
        rig.ack()
        rig.rec.tick()
        _time.sleep(0.25)
    assert rig.rec.capture.running, "recording"
    last_beat = _time.monotonic()
    stopped = None
    while _time.monotonic() - last_beat < 4:
        rig.rec.tick()
        if not rig.rec.capture.running:
            stopped = _time.monotonic()
            break
        _time.sleep(0.05)
    assert stopped is not None and stopped - last_beat <= 2.0, stopped and stopped - last_beat
    [name] = [n for n in os.listdir(rig.spool.ready) if n.endswith(".opus")]
    path = rig.spool.ready / name
    assert _band_rms(path, 0, 600) > _band_rms(path, 0, 2700) + 20
    assert _band_rms(path, 1, 2700) > _band_rms(path, 1, 600) + 20
    assert (rig.spool.ready / (name + ".sha256")).read_text().split()[0] == mr.sha256_of(path)


# ── TC-1407: the shipped unit and files ──────────────────────────────────────

def _directives(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_tc1407_the_unit_runs_as_its_own_account_with_audio_and_no_network():
    unit = _directives(WALL / "wall-meeting-recorder.service")
    for directive in ("User=wall-meeting", "Group=wall-meeting", "SupplementaryGroups=audio",
                      "ExecStart=/usr/bin/python3 /usr/local/lib/wall-panel/wall-meeting-recorder.py",
                      "Restart=always", "KillMode=mixed", "RuntimeDirectory=wall-meeting-recorder",
                      "RuntimeDirectoryMode=0755", "ReadWritePaths=/var/spool/wall-meeting",
                      "ProtectSystem=strict", "RestrictAddressFamilies=AF_UNIX", "NoNewPrivileges=true",
                      "CapabilityBoundingSet=", "WantedBy=multi-user.target"):
        assert directive in unit, directive


def test_tc1407_the_knobs_are_documented_and_recording_defaults_off():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    for line in ("WALL_MEETING_RECORDING=false", "WALL_MEETING_PULL_PUBKEY=", "WALL_MEETING_CALL_GATEWAYS=",
                 "WALL_MEETING_SPOOL_MAX_MB=512", "WALL_MEETING_MIN_FREE_MB=5120",
                 "WALL_MEETING_MAX_MINUTES=240", "WALL_MEETING_BITRATE_KBPS=48"):
        assert re.search("^%s$" % re.escape(line), env, re.M), line
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert ': "${WALL_MEETING_RECORDING:=false}"' in firstboot
    for key, value in mr.DEFAULTS.items():
        assert ': "${%s:=%d}"' % (key, value) in firstboot, key


def test_tc1407_new_files_are_lf_and_the_scripts_are_executable_in_git():
    for name in ("wall-meeting-recorder.py", "wall-meeting-recorder.service", "wall-meeting-spool.py"):
        assert b"\r" not in (WALL / name).read_bytes(), name
    for name in ("wall-meeting-recorder.py", "wall-meeting-spool.py"):
        out = subprocess.run(["git", "-C", str(WALL), "ls-files", "-s", name],
                             capture_output=True, text=True, check=True).stdout
        assert out.startswith("100755"), (name, out)


def test_tc1407_the_renderer_directory_rule_names_the_new_files():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    rule = re.search(r"cat > /etc/tmpfiles.d/wall-panel-renderer.conf <<'EOF'\n(.*?)\nEOF\n", firstboot, re.S).group(1)
    assert "recorder's request and indicator acknowledgement" in rule
    assert "d /run/wall-panel-renderer 0755 panel panel -" in rule
    assert mr.REQUEST_FILE.parent == mr.ACK_FILE.parent == Path("/run/wall-panel-renderer")


# ── TC-1408: the firstboot block, executed ───────────────────────────────────

KEY = "ssh-ed25519 " + "A" * 68 + " homehub-meeting-pull"


def block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN meeting-recorder\n(.*?)# END meeting-recorder\n", text, re.S)
    assert found, "wall-firstboot.sh lost its meeting-recorder block"
    return found.group(1)


def run_block(tmp_path, *, knob="true", pubkey=KEY, same=False, gateways="desktop-office"):
    calls = tmp_path / "calls"
    etc = tmp_path / "etc"
    keys = tmp_path / "keys"
    etc.mkdir(exist_ok=True)
    keys.mkdir(exist_ok=True)
    rec = 'printf "%s\\n" "{kind} $*" >> "' + calls.as_posix() + '"'
    text = block().replace("/etc/wall-panel/meeting-recorder.env", (etc / "meeting-recorder.env").as_posix())
    text = text.replace("/var/lib/wall-meeting/.ssh/authorized_keys", (keys / "authorized_keys").as_posix())
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD="{}"'.format(WALL.as_posix()),
        ("WALL_MEETING_RECORDING='{}'".format(knob) if knob is not None else "unset WALL_MEETING_RECORDING"),
        "WALL_MEETING_PULL_PUBKEY='{}'".format(pubkey),
        "WALL_MEETING_CALL_GATEWAYS='{}'".format(gateways),
        'log() { echo "[wall-firstboot] $*"; }',
        'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }',
        'fail_step() { echo "[wall-firstboot] ERROR: $*" >&2; %s; }' % rec.format(kind="fail_step"),
        'install() { %s; return 0; }' % rec.format(kind="install"),
        'getent() { return 0; }',
        'id() { return 0; }',
        'usermod() { %s; }' % rec.format(kind="usermod"),
        'chown() { %s; }' % rec.format(kind="chown"),
        'cmp() { return %d; }' % (0 if same else 1),
        'systemctl() { %s; return 0; }' % rec.format(kind="systemctl"),
        'enable_unit_now() { shift; %s; }' % rec.format(kind="enable_unit_now"),
        text,
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines, etc / "meeting-recorder.env", keys / "authorized_keys"


def _ok(done):
    assert done.returncode == 0 and "BLOCK-DONE" in done.stdout, done.stdout + done.stderr


@needs_bash
def test_tc1408_knob_on_installs_renders_writes_the_key_and_starts(tmp_path):
    done, calls, env, keys = run_block(tmp_path)
    _ok(done)
    p = WALL.as_posix()
    installs = [c for c in calls if c.startswith("install")]
    for expected in (
            "install -d -o wall-meeting -g wall-meeting -m 0700 /var/spool/wall-meeting",
            "install -m 0755 {}/wall-meeting-recorder.py /usr/local/lib/wall-panel/wall-meeting-recorder.py".format(p),
            "install -m 0755 {}/wall-meeting-spool.py /usr/local/lib/wall-panel/wall-meeting-spool".format(p),
            "install -m 0644 {}/wall-meeting-recorder.service /etc/systemd/system/wall-meeting-recorder.service".format(p)):
        assert expected in installs, expected
    assert "usermod -p * wall-meeting" in calls
    assert "enable_unit_now wall-meeting-recorder.service" in calls
    assert "systemctl restart wall-meeting-recorder.service" in calls
    assert [c for c in calls if c.startswith("fail_step")] == []
    rendered = env.read_text()
    assert "WALL_MEETING_CALL_GATEWAYS=desktop-office" in rendered and "WALL_MEETING_SPOOL_MAX_MB=512" in rendered
    assert keys.read_text() == 'restrict,command="/usr/local/lib/wall-panel/wall-meeting-spool" %s\n' % KEY


@needs_bash
def test_tc1408_the_default_is_off_and_off_disables(tmp_path):
    for knob in (None, "false"):
        done, calls, _env, _keys = run_block(tmp_path, knob=knob)
        _ok(done)
        assert "systemctl disable --now wall-meeting-recorder.service" in calls
        assert not any(c.startswith("enable_unit_now") for c in calls)
        (tmp_path / "calls").unlink()


@needs_bash
def test_tc1408_an_invalid_knob_fails_the_step_and_starts_nothing(tmp_path):
    done, calls, _env, _keys = run_block(tmp_path, knob="yes")
    _ok(done)
    [failure] = [c for c in calls if c.startswith("fail_step")]
    assert "WALL_MEETING_RECORDING must be exactly true or false" in failure
    assert not any(c.startswith(("enable_unit_now", "systemctl disable")) for c in calls)


@needs_bash
def test_tc1408_no_key_means_nothing_can_collect(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "authorized_keys").write_text("old\n")
    done, calls, _env, keys_file = run_block(tmp_path, pubkey="")
    _ok(done)
    assert not keys_file.exists()
    assert [c for c in calls if c.startswith("fail_step")] == []


@needs_bash
@pytest.mark.parametrize("bad", ["ssh-rsa " + "A" * 68, "ssh-ed25519 short", KEY + " extra words",
                                 'ssh-ed25519 %s x"; rm -rf /' % ("A" * 68)])
def test_tc1408_a_malformed_key_is_a_failed_step_and_no_key_line(tmp_path, bad):
    done, calls, _env, keys_file = run_block(tmp_path, pubkey=bad.replace("'", ""))
    _ok(done)
    assert not keys_file.exists()
    [failure] = [c for c in calls if c.startswith("fail_step")]
    assert "is not one ssh-ed25519 public key" in failure


@needs_bash
def test_tc1408_unchanged_files_and_settings_do_not_restart(tmp_path):
    run_block(tmp_path, same=True)
    (tmp_path / "calls").unlink()
    done, calls, _env, _keys = run_block(tmp_path, same=True)
    _ok(done)
    assert "systemctl restart wall-meeting-recorder.service" not in calls
    assert "enable_unit_now wall-meeting-recorder.service" in calls
