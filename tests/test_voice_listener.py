"""The hub voice listener (SN-056, SR-163..SR-169).

What runs here, on the dev PC, with no hub, no relay and no engine: the three
locks on the room's sound and every other refusal; the loudness rule and the
background level; the activity gate and its pre-roll; the duty ceiling; the
presence reading and the arm decision; the listening session against
EntertainmentPC Core's timings; the whole pipeline with stand-in engines
(audio made in the test: tones, noise and silence); the utterance document
and the action client against a stand-in of MINI-SERV's interface that applies
that interface's own rules; the model pin and the wake phrase's tokens; the
reader's argv; the listener loop; that no transcript reaches the journal, the
status file or the feed; and the unit/template/lock contract.

What does not: sherpa-onnx, the models, the relay and MINI-SERV. The real
engines are tests/test_voice_listener_engines.py (opt-in, synthetic speech);
the hub is HomeHub docs/HUB_VOICE_LISTENER_TEST_PLAN_2026-11-11.md.
Implements: TC-1780..TC-1800, TC-1803, TC-1807.
"""

from __future__ import annotations

import http.server
import json
import math
import re
import struct
import sys
import threading
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
HOME = STACK / "voice-listener"
for sub in ("voice-listener", "camera-presence", "camera-relay", "camera-motion", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import door_relay as dr  # noqa: E402
import voice_engines as ve  # noqa: E402
import voice_gate as vg  # noqa: E402
import voice_listener as vl  # noqa: E402
import voice_post as vp  # noqa: E402
import voice_session as vs  # noqa: E402

TOKEN = "t0ken-" + "A1b2C3d4" * 5          # 46 visible characters, fictional
SAID = "show the purple elephant games"     # a transcript that must never leak
PEER, HUB = "192.0.2.50", "192.0.2.10"      # documentation addresses


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── audio made here: nothing recorded, nothing spoken ───────────────────────


def tone(ms, dbfs=-20.0, hz=440.0):
    """A sine at `dbfs` RMS, 16 kHz mono signed 16-bit."""
    peak = vg.from_dbfs(dbfs) * math.sqrt(2)
    n = vg.SAMPLE_RATE * ms // 1000
    return b"".join(struct.pack("<h", max(-32768, min(32767, int(peak * math.sin(2 * math.pi * hz * i / vg.SAMPLE_RATE)))))
                    for i in range(n))


def quiet(ms, dbfs=-70.0):
    return tone(ms, dbfs, 97.0)


def frames_of(audio):
    return [audio[i:i + vg.FRAME_BYTES] for i in range(0, len(audio) - vg.FRAME_BYTES + 1, vg.FRAME_BYTES)]


MARK = tone(vg.FRAME_MS, -12.0, 1000.0)     # the stand-in wake word "hears" this exact frame


class FakeWake:
    """Fires on the frame after it has seen MARK; counts what it was given."""

    def __init__(self, fail_after=None):
        self.frames, self.resets, self.fail_after, self._armed = [], 0, fail_after, False

    def process(self, frame):
        self.frames.append(bytes(frame))
        if self.fail_after is not None and len(self.frames) > self.fail_after:
            raise RuntimeError("engine fault")
        fired, self._armed = self._armed, bytes(frame) == MARK
        return fired

    def reset(self):
        self.resets += 1
        self._armed = False


class FakeStt:
    def __init__(self, text=SAID):
        self.text, self.audio_bytes, self.handed = text, [], None

    def transcribe(self, audio):
        self.audio_bytes.append(len(audio))
        self.handed = audio              # kept only to see that the pipeline zeroes it afterwards
        return self.text


class Posts(list):
    def __call__(self, text, wake_checked):
        self.append((text, wake_checked))
        return "started"


def pipeline(wake=None, stt=None, post=None, pre_roll_ms=0, duty=100, gate=True, hold_after_post_ms=0):
    session = vs.ListenSession(vs.SessionOptions(pre_roll_ms=pre_roll_ms, hold_after_post_ms=hold_after_post_ms))
    return vs.Pipeline(vg.ActivityGate(enabled=gate), vg.DutyLimiter(duty), session, wake or FakeWake(),
                       stt or FakeStt(), post if post is not None else Posts())


def play(p, audio, start_ms=0):
    """Feed audio frame by frame on a clock that counts the audio; all events."""
    events, now = [], start_ms
    for frame in frames_of(audio):
        now += vg.FRAME_MS
        events += p.feed(bytearray(frame), now)
    return events, now


REQUEST = quiet(1600) + tone(320, -22.0) + MARK + tone(1200, -20.0, 300.0) + quiet(1600)


# ── TC-1780 settings ─────────────────────────────────────────────────────────


def env(**over):
    base = {"LAN_IP": HUB, "MAIN_BOX_IP": PEER}
    base.update({(k if k.isupper() and not k.startswith("X_") else k): v for k, v in over.items()})
    return {k: v for k, v in base.items() if v is not None}


def listener_env(**over):
    return env(**{("CAMERA_LIVINGROOM_LISTENER_" + k if not k.startswith(("MINISERV_", "LAN_", "MAIN_", "CAMERA_"))
                   else k): v for k, v in over.items()})


def settings(raw=None, **over):
    return vl.ListenerSettings(listener_env(**over), "livingroom", raw)


def test_tc1780_defaults_are_off_deaf_and_core_timings():
    s = settings()
    assert s.enabled is False and s.source == "none" and s.ruling == "" and s.hears_room is False
    assert s.presence_policy == "required" and s.gate is True and s.duty_max_pct == 100
    assert (s.pre_roll_ms, s.end_silence_ms, s.no_speech_ms, s.max_utterance_ms) == (0, 700, 3000, 8000)
    assert s.wake_tokens == "^HE Y ^B RU N O" and s.wake_label == "HEY_BRUNO" and s.wake_streams == 1
    assert s.feed_port == 0 and s.peer_port == 0 and s.token == "" and s.peer_host == PEER
    assert s.presence_event == "living_room.presence" and s.relay_query == "?audio"
    assert s.budget_pct == 5 and s.budget_armed_pct == 20


@pytest.mark.parametrize("key,value", [
    ("SOURCE", "microphone"), ("ROOM_AUDIO_RULING", "yes"), ("ROOM_AUDIO_RULING", "ratified-soon"),
    ("PRESENCE", "optional"), ("GATE", "on"), ("DUTY_MAX_PCT", "0"), ("DUTY_MAX_PCT", "150"),
    ("PRE_ROLL_MS", "9000"), ("WAKE_STREAMS", "9"), ("WAKE_TOKENS", "hey bruno"), ("WAKE_LABEL", "hey"),
    ("FEED_PORT", "80"), ("FEED_PEERS", "minipc"), ("RELAY_TRACKS", "picture"),("PRESENCE_STALE_SEC", "10"),
    ("GATE_MIN_SPEECH_DBFS", "loud"), ("MINISERV_ACTION_PORT", "seventy"), ("UNTIL", "2026-11-20"),
    ("UNTIL", "2026-13-45T00:00:00Z"),
])
def test_tc1780_a_malformed_knob_is_refused_by_key_never_by_value(key, value):
    with pytest.raises(vl.Invalid) as err:
        settings(**{key: value})
    message = str(err.value)
    assert (key if key.startswith("MINISERV_") else "CAMERA_LIVINGROOM_LISTENER_" + key) in message
    assert value not in message or value in ("yes", "on", "hey", "0")   # short words may sit in the rule's own text


def test_tc1780_an_end_date_is_a_utc_instant_or_nothing():
    assert settings().until is None
    assert settings(UNTIL="2027-01-15T08:00:10Z").until == 1_800_000_010.0


def test_tc1780_the_token_is_read_raw_and_never_shown():
    s = settings(raw={vl.TOKEN_KEY: TOKEN + "\r"})
    assert s.token == TOKEN and TOKEN not in repr(s.__dict__.get("camera")) and vp.token_ok(s.token)
    assert settings(SOURCE="relay", ROOM_AUDIO_RULING="ratified-2026-12-01").hears_room is True


# ── TC-1781 the locks on the room's sound ────────────────────────────────────


RELAY_ON = {"CAMERA_LIVINGROOM_RELAY_ENABLED": "true", "CAMERA_LIVINGROOM_RELAY_AUDIO": "true"}


def test_tc1781_no_source_and_no_ruling_are_refusals_that_name_the_knob():
    e = listener_env()
    assert vl.refusals(vl.ListenerSettings(e, "livingroom"), e) == [
        "CAMERA_LIVINGROOM_LISTENER_SOURCE is none: no audio source is configured"]
    e = listener_env(SOURCE="relay", MINISERV_ACTION_PORT="8571", **RELAY_ON)
    reasons = vl.refusals(vl.ListenerSettings(e, "livingroom", {vl.TOKEN_KEY: TOKEN}), e)
    assert len(reasons) == 1 and reasons[0].startswith("CAMERA_LIVINGROOM_LISTENER_ROOM_AUDIO_RULING is empty")
    assert "Owner's ruling" in reasons[0]


def test_tc1781_with_the_ruling_named_every_other_gap_is_still_a_refusal():
    e = listener_env(SOURCE="relay", ROOM_AUDIO_RULING="ratified-2026-12-01")
    e["MAIN_BOX_IP"] = ""
    reasons = " | ".join(vl.refusals(vl.ListenerSettings(e, "livingroom"), e))
    for key in ("CAMERA_LIVINGROOM_RELAY_ENABLED", "CAMERA_LIVINGROOM_RELAY_AUDIO", "MAIN_BOX_IP",
                "MINISERV_ACTION_PORT", "MINISERV_ACTION_TOKEN"):
        assert key in reasons
    full = listener_env(SOURCE="relay", ROOM_AUDIO_RULING="ratified-2026-12-01", MINISERV_ACTION_PORT="8571",
                        **RELAY_ON)
    assert vl.refusals(vl.ListenerSettings(full, "livingroom", {vl.TOKEN_KEY: TOKEN}), full) == []


def write_wav(path, audio):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(vg.SAMPLE_RATE)
        handle.writeframes(audio)
    return str(path)


def test_tc1781_the_file_source_needs_a_wav_and_no_ruling(tmp_path):
    e = listener_env(SOURCE="file")
    assert "LISTENER_FILE is not set" in vl.refusals(vl.ListenerSettings(e, "livingroom"), e)[0]
    e = listener_env(SOURCE="file", FILE=str(tmp_path / "missing.wav"))
    assert "missing or not a WAV" in vl.refusals(vl.ListenerSettings(e, "livingroom"), e)[0]
    e = listener_env(SOURCE="file", FILE=write_wav(tmp_path / "ok.wav", tone(1500)))
    assert vl.refusals(vl.ListenerSettings(e, "livingroom"), e) == []


def write_env(tmp_path, **over):
    path = tmp_path / ".env"
    path.write_text("".join("%s=%s\n" % kv for kv in listener_env(**over).items()), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("over", [
    {},                                                                       # as shipped: no source
    {"ENABLED": "true"},                                                      # the knob alone
    {"ENABLED": "true", "SOURCE": "relay", "MINISERV_ACTION_PORT": "8571",    # everything but the ruling
     "CAMERA_LIVINGROOM_RELAY_ENABLED": "true", "CAMERA_LIVINGROOM_RELAY_AUDIO": "true"},
])
def test_tc1781_run_refuses_before_any_reader_engine_or_socket(tmp_path, monkeypatch, over):
    def forbidden(*_a, **_k):
        raise AssertionError("nothing may start before the locks are open")
    for name in ("build_engines",):
        monkeypatch.setattr(vl, name, forbidden)
    monkeypatch.setattr(vl.door_relay, "start_consumer", forbidden)
    monkeypatch.setattr(vl.door_relay, "remove_containers", forbidden)
    monkeypatch.setattr(vl.subprocess, "run", forbidden)
    monkeypatch.setattr(vl.subprocess, "Popen", forbidden)
    monkeypatch.setattr(vl.vp.ActionClient, "get_state", forbidden)
    logged = []
    monkeypatch.setattr(vl.mc, "log", lambda m, warning=False: logged.append(m))
    path = write_env(tmp_path, **over)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("%s=%s\n" % (vl.TOKEN_KEY, TOKEN))
    assert vl.run_service(path, "livingroom", str(tmp_path / "state"), str(tmp_path / "run")) == vl.EXIT_REFUSED
    assert "REFUSED" in logged[-1] and TOKEN not in " ".join(logged)
    assert not (tmp_path / "state").exists() and not (tmp_path / "run").exists()


def test_tc1781_past_its_end_date_run_starts_nothing_and_ends_cleanly(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("nothing may start after the window ended")
    monkeypatch.setattr(vl, "build_engines", forbidden)
    monkeypatch.setattr(vl.door_relay, "start_consumer", forbidden)
    monkeypatch.setattr(vl.subprocess, "Popen", forbidden)
    logged = []
    monkeypatch.setattr(vl.mc, "log", lambda m, warning=False: logged.append((m, warning)))
    path = write_env(tmp_path, ENABLED="true", SOURCE="relay", ROOM_AUDIO_RULING="ratified-2026-12-01",
                     UNTIL="2026-01-01T00:00:00Z")
    assert vl.run_service(path, "livingroom", str(tmp_path / "state"), str(tmp_path / "run")) == 0
    assert "the listening window is over" in logged[-1][0] and logged[-1][1] is False
    assert not (tmp_path / "state").exists()


def test_tc1781_condition_is_true_only_for_exactly_true(tmp_path, monkeypatch):
    monkeypatch.setattr(vl.mc, "log", lambda *a, **k: None)
    assert vl.main(["--env-file", write_env(tmp_path), "--camera", "livingroom", "condition"]) == 1
    assert vl.main(["--env-file", write_env(tmp_path, ENABLED="yes"), "--camera", "livingroom", "condition"]) == 1
    assert vl.main(["--env-file", write_env(tmp_path, ENABLED="true"), "--camera", "livingroom", "condition"]) == 0


# ── TC-1782 levels and the background ────────────────────────────────────────


def test_tc1782_rms_is_the_same_with_and_without_audioop():
    for audio in (tone(80, -20.0), tone(80, -55.0, 1000.0), bytes(vg.FRAME_BYTES), tone(80, -4.0)):
        assert abs(vg.frame_rms(audio) - vg.rms_pure(audio)) <= 1.0
    assert abs(vg.to_dbfs(vg.frame_rms(tone(80, -20.0))) + 20.0) < 0.3
    assert vg.frame_rms(b"") == 0.0 and vg.to_dbfs(0) == vg.SILENT_DBFS
    assert abs(vg.to_dbfs(vg.from_dbfs(-37.5)) + 37.5) < 1e-6
    assert vg.bytes_for(80) == vg.FRAME_BYTES and vg.ms_for(vg.FRAME_BYTES) == 80


def test_tc1782_the_background_falls_fast_and_rises_slowly():
    floor = vg.NoiseFloor()
    floor.learn(100.0)
    for _ in range(25):            # two seconds of something ten times louder
        floor.learn(1000.0)
    assert 200 < floor.level < 300          # it has barely followed: about 18 % of the way after 2 s
    for _ in range(25):            # two seconds of quiet again
        floor.learn(100.0)
    assert floor.level < 130                # and comes back down quickly


def test_tc1782_steady_sound_stops_counting_after_about_five_seconds():
    floor = vg.NoiseFloor()
    floor.learn(vg.from_dbfs(-70))
    loud = vg.from_dbfs(-25)
    counted = 0
    for _ in range(125):           # ten seconds of one steady level
        counted += 1 if loud >= floor.speech_level(-50, 8) else 0
        floor.learn(loud)
    assert 4.0 <= counted * vg.FRAME_MS / 1000.0 <= 6.0


# ── TC-1783 the activity gate ────────────────────────────────────────────────


def run_gate(gate, audio, start_ms=0):
    out, changes, now = [], [], start_ms
    for frame in frames_of(audio):
        now += vg.FRAME_MS
        passed, change = gate.feed(frame, vg.frame_rms(frame), now)
        out += [bytes(p) for p in passed]
        if change:
            changes.append((change, now))
    return out, changes, now


def test_tc1783_quiet_and_a_single_click_never_open_it():
    gate = vg.ActivityGate()
    out, changes, _ = run_gate(gate, quiet(3040) + tone(80, -10.0) + quiet(2000))     # a click inside one frame
    assert out == [] and changes == [] and gate.opens == 0 and gate.open_ms == 0


def test_tc1783_sound_opens_it_and_the_pre_roll_comes_first():
    gate = vg.ActivityGate()
    speech = tone(800, -20.0)
    out, changes, _ = run_gate(gate, quiet(2000) + speech + quiet(3000))
    assert [c for c, _t in changes] == ["opened", "closed"] and gate.opens == 1
    assert out[0] != frames_of(speech)[0]                    # what came first is from before the sound
    assert frames_of(speech)[0] in out and frames_of(speech)[-1] in out      # and none of the sound is lost
    opened, closed = changes[0][1], changes[1][1]
    assert opened == 2000 + 2 * vg.FRAME_MS                  # two frames of sound open it
    assert closed - (2000 + 800) == gate.hang_ms             # it closes hang_ms after the last sound
    assert len(out) * vg.FRAME_MS <= 480 + 800 + gate.hang_ms + vg.FRAME_MS      # and nothing after that passed
    fresh = vg.ActivityGate()
    run_gate(fresh, quiet(2000) + tone(160, -20.0))
    assert fresh.is_open and fresh.holds_no_audio            # the pre-roll was handed over and zeroed at the opening


def test_tc1783_the_pre_roll_is_bounded_and_wiped():
    gate = vg.ActivityGate(pre_roll_ms=480)
    run_gate(gate, quiet(5000))
    assert len(gate.pre_roll) == vg.bytes_for(480) and not gate.holds_no_audio
    gate.release()
    assert gate.holds_no_audio and len(gate.pre_roll) == 0
    buffer = vg.WipedBuffer(8)
    buffer.write(b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a")
    taken = buffer.take()
    assert bytes(taken) == b"\x03\x04\x05\x06\x07\x08\x09\x0a" and buffer.is_cleared
    vg.wipe_bytes(taken)
    assert bytes(taken) == bytes(8)


def test_tc1783_a_steady_sound_closes_it_again_by_itself():
    gate = vg.ActivityGate()
    _out, changes, _ = run_gate(gate, quiet(2000) + tone(20000, -25.0))
    assert [c for c, _t in changes] == ["opened", "closed"]
    assert 4000 <= changes[1][1] - changes[0][1] <= 8000      # about five seconds, plus the hang
    assert gate.open_ms < 9000 and gate.closed_ms > 12000


def test_tc1783_switched_off_it_passes_everything():
    gate = vg.ActivityGate(enabled=False)
    out, changes, _ = run_gate(gate, quiet(1000))
    assert len(out) == len(frames_of(quiet(1000))) and changes == [] and gate.holds_no_audio


# ── TC-1784 the duty ceiling ─────────────────────────────────────────────────


def test_tc1784_the_duty_ceiling_holds_over_a_minute():
    limiter = vg.DutyLimiter(25, burst_ms=4000)
    allowed = sum(1 for i in range(750) if limiter.allow(i * vg.FRAME_MS))     # a minute of frames
    spent = allowed * vg.FRAME_MS
    assert 60000 * 0.25 <= spent <= 60000 * 0.25 + 4000 + vg.FRAME_MS
    assert limiter.refused_ms == (750 - allowed) * vg.FRAME_MS and limiter.spent_ms == spent
    assert not limiter.ready(60000) and limiter.ready(60000 + 8000)


def test_tc1784_a_hundred_percent_is_no_limit():
    limiter = vg.DutyLimiter(100)
    assert all(limiter.allow(i * vg.FRAME_MS) for i in range(1000)) and limiter.refused_ms == 0 and limiter.ready(0)


# ── TC-1785 the presence reading ─────────────────────────────────────────────

NOW_MS = 1_800_000_000_000


def presence_status(present=True, age_sec=5, **over):
    record = {"known": True, "last_frame_utc_ms": NOW_MS - int(age_sec * 1000),
              "state": [{"v": 1, "type": "state", "event": "living_room.presence", "present": present}]}
    record.update(over)
    return record


@pytest.mark.parametrize("status,known,present", [
    (None, False, False), ("not a record", False, False), ({}, False, False),
    (presence_status(True), True, True), (presence_status(False), True, False),
    (presence_status(True, age_sec=151), False, False),                       # stale
    (presence_status(True, age_sec=-120), False, False),                      # a clock gone wrong
    (presence_status(True, known=False), False, False),                       # no frames judged yet
    (presence_status(True, stopped_utc_ms=NOW_MS), False, False),             # the detector stopped
    (presence_status(True, state=[{"event": "living_room.engage", "engaged": True}]), False, False),
    (presence_status(True, last_frame_utc_ms=None), False, False),
    (presence_status(True, last_frame_utc_ms=True), False, False),
])
def test_tc1785_presence_is_known_only_from_a_fresh_running_detector(status, known, present):
    reading = vg.read_presence(status, NOW_MS, 150)
    assert (reading.known, reading.present) == (known, present)


# ── TC-1786 the arm decision ─────────────────────────────────────────────────

HERE_NOW = vg.PresenceReading(True, True, 3)
EMPTY = vg.PresenceReading(True, False, 3)
READY = vg.PeerState(True, "ready", "off")


@pytest.mark.parametrize("hold,peer,presence,policy,expected", [
    (True, READY, HERE_NOW, "required", (False, vg.DISARM_HOLD)),
    (False, None, HERE_NOW, "required", (False, vg.DISARM_PEER_UNREACHABLE)),
    (False, vg.PeerState(), HERE_NOW, "required", (False, vg.DISARM_PEER_UNREACHABLE)),
    (False, vg.PeerState(True, "ready", "muted"), HERE_NOW, "required", (False, vg.DISARM_PEER_MUTED)),
    (False, vg.PeerState(True, "ready", "armed"), HERE_NOW, "required", (False, vg.DISARM_PEER_LISTENING)),
    (False, READY, None, "required", (False, vg.DISARM_PRESENCE_UNKNOWN)),
    (False, READY, vg.PresenceReading(), "required", (False, vg.DISARM_PRESENCE_UNKNOWN)),
    (False, READY, EMPTY, "required", (False, vg.DISARM_ROOM_EMPTY)),
    (False, READY, HERE_NOW, "required", (True, vg.ARMED)),
    (False, vg.PeerState(True, "up", "absent"), HERE_NOW, "required", (True, vg.ARMED)),
    (False, READY, None, "ignored", (True, vg.ARMED)),
    (False, READY, EMPTY, "ignored", (True, vg.ARMED)),
    (True, READY, EMPTY, "ignored", (False, vg.DISARM_HOLD)),
])
def test_tc1786_the_listener_hears_only_when_every_rule_allows(hold, peer, presence, policy, expected):
    assert vg.arm_decision(hold, peer, presence, policy) == expected


def test_tc1786_the_synthetic_source_does_not_ask_the_peer():
    assert vg.arm_decision(False, None, HERE_NOW, "required", peer_required=False) == (True, vg.ARMED)
    assert vg.arm_decision(False, None, EMPTY, "required", peer_required=False) == (False, vg.DISARM_ROOM_EMPTY)


# ── TC-1787 the listening session (Core's timings) ───────────────────────────


def capture_all(session, audio, speech, start_ms=0):
    now = start_ms
    for frame in frames_of(audio):
        now += vg.FRAME_MS
        end = session.capture(frame, speech, now)
        if end:
            return end, now
    return None, now


def test_tc1787_quiet_after_speech_ends_the_request():
    session = vs.ListenSession()
    session.wake(0)
    assert session.state == vs.WAKE_HEARD
    end, now = capture_all(session, tone(1200), True)
    assert end is None and session.state == vs.LISTENING
    end, later = capture_all(session, quiet(2000), False, now)
    assert end.reason == vs.END_SILENCE and later - now == 720          # 700 ms, in whole 80 ms frames
    assert len(end.audio) == vg.bytes_for(1200 + 720) and session.state == vs.IDLE and session.holds_no_audio


def test_tc1787_nothing_said_after_the_wake_word_gives_up_with_no_audio():
    session = vs.ListenSession()
    session.wake(0)
    end, now = capture_all(session, quiet(5000), False)
    assert end.reason == vs.END_NO_SPEECH and end.audio is None and now == 3040 and session.holds_no_audio


def test_tc1787_the_longest_request_is_eight_seconds():
    session = vs.ListenSession()
    session.wake(0)
    end, _ = capture_all(session, tone(9000), True)
    assert end.reason == vs.END_MAX_LENGTH and len(end.audio) == vg.bytes_for(8000)


def test_tc1787_audio_that_stops_ends_it_with_or_without_speech():
    session = vs.ListenSession()
    session.wake(0)
    capture_all(session, tone(400), True)
    assert session.stalled(1500) is None
    end = session.stalled(400 + 2000)
    assert end.reason == vs.END_AUDIO_STALLED and end.audio is not None
    session.wake(10000)
    end = session.stalled(12000)
    assert end.reason == vs.END_AUDIO_STALLED and end.audio is None and session.holds_no_audio


def test_tc1787_pre_roll_puts_the_wake_phrase_in_front_only_when_asked():
    plain = vs.ListenSession()
    for frame in frames_of(tone(2000)):
        plain.idle_frame(frame)
    assert plain.holds_no_audio and plain.includes_wake_phrase is False
    rolling = vs.ListenSession(vs.SessionOptions(pre_roll_ms=1500))
    for frame in frames_of(tone(3000)):
        rolling.idle_frame(frame)
    assert rolling.buffered_bytes == vg.bytes_for(1500) and rolling.includes_wake_phrase is True
    rolling.wake(0)
    end, _ = capture_all(rolling, tone(400), True)
    end, _ = capture_all(rolling, quiet(1000), False, 400)
    assert len(end.audio) == vg.bytes_for(1500 + 400 + 720)
    with pytest.raises(ValueError):
        vs.SessionOptions(pre_roll_ms=5000)


# ── TC-1788 the whole pipeline with stand-in engines ────────────────────────


def test_tc1788_one_request_is_heard_transcribed_and_posted_once():
    wake, stt, posts = FakeWake(), FakeStt(), Posts()
    p = pipeline(wake, stt, posts)
    events, _ = play(p, REQUEST)
    assert posts == [(SAID, True)]                           # no pre-roll: the hub vouches for the wake word
    assert [e["show"] for e in events] == ["hearing", "listening", "working", "armed"]
    assert events[-1] == {"show": "armed", "outcome": "started"}
    assert p.counts["wake_fires"] == 1 and p.counts["requests"] == 1 and p.counts["posted"] == 1
    assert p.outcomes == {"started": 1} and p.flush_source is True
    assert stt.audio_bytes and stt.audio_bytes[0] <= vg.bytes_for(8000)
    assert stt.audio_bytes[0] > vg.bytes_for(1000) and len(stt.handed) == 0      # the request's sound is gone once read
    assert SAID not in json.dumps([events, p.counts, p.outcomes])
    p.disarm()
    assert p.holds_no_audio


def test_tc1788_a_second_request_is_heard_whether_or_not_the_caller_cleared_the_flush_flag():
    posts = Posts()
    p = pipeline(post=posts)
    _events, now = play(p, REQUEST)
    assert p.flush_source is True                      # the caller's cue to drop what queued up; not a mute
    play(p, REQUEST, now)
    assert len(posts) == 2 and p.counts["wake_fires"] == 2


def test_tc1788_working_is_shown_before_the_request_is_transcribed_not_after():
    seen = []

    class WatchingStt(FakeStt):
        def transcribe(self, audio):
            seen.append(("transcribing while shown as", p.show, list(told)))
            return super().transcribe(audio)
    told = []
    p = pipeline(stt=WatchingStt())
    p.on_show = lambda: told.append(p.show)
    play(p, REQUEST)
    assert seen == [("transcribing while shown as", "working", ["hearing", "listening", "working"])]
    assert told == ["hearing", "listening", "working", "armed"]
    p.on_show = lambda: 1 / 0                      # a reporter that fails must not stop the hearing
    _events, now = play(p, REQUEST, 20000)
    assert p.counts["posted"] == 2 and p.counts["engine_errors"] == 0


def test_tc1788_the_engine_is_not_fed_while_the_room_is_quiet():
    wake = FakeWake()
    p = pipeline(wake)
    play(p, quiet(10000))
    assert wake.frames == [] and p.counts["gate_opens"] == 0
    ungated = FakeWake()
    play(pipeline(ungated, gate=False), quiet(10000))
    assert len(ungated.frames) == 125


def test_tc1788_with_pre_roll_the_peer_is_asked_to_check_the_wake_phrase():
    posts = Posts()
    play(pipeline(post=posts, pre_roll_ms=1500), REQUEST)
    assert posts == [(SAID, False)]


def test_tc1788_nothing_heard_posts_nothing():
    posts = Posts()
    p = pipeline(stt=FakeStt(""), post=posts)
    events, _ = play(p, REQUEST)
    assert posts == [] and p.counts["nothing_heard"] == 1 and events[-1] == {"show": "armed", "reason": "nothing_heard"}


def test_tc1788_after_a_post_bruno_s_own_reply_is_not_heard():
    wake, posts = FakeWake(), Posts()
    p = pipeline(wake, post=posts, hold_after_post_ms=3000)
    _events, now = play(p, REQUEST)
    fed = len(wake.frames)
    play(p, tone(240, -22.0) + MARK + tone(800, -20.0) + quiet(1200), now)      # inside the three seconds
    assert len(wake.frames) == fed and len(posts) == 1


def test_tc1788_an_engine_that_throws_ends_the_request_not_the_service():
    wake = FakeWake(fail_after=3)
    p = pipeline(wake)
    events, _ = play(p, quiet(1600) + tone(2000, -20.0))
    assert p.counts["engine_errors"] >= 1 and p.engine_errors_in_a_row >= 1
    assert {"show": "armed", "reason": "engine_error"} in events and p.session.state == vs.IDLE
    p.disarm()
    assert p.holds_no_audio


def test_tc1788_the_duty_ceiling_stops_the_engine_and_says_so():
    wake = FakeWake()
    p = pipeline(wake)
    p.limiter = vg.DutyLimiter(10, burst_ms=800)
    events, _ = play(p, quiet(1600) + tone(6000, -20.0, 300.0))
    assert p.counts["duty_stops"] >= 1 and {"show": "armed", "reason": "duty"} in events
    assert len(wake.frames) * vg.FRAME_MS <= 800 + 0.10 * 7600 + vg.FRAME_MS
    assert p.limiter.refused_ms >= 3000          # the time it was deaf is counted, not only the frame that stopped it


# ── TC-1789 the utterance document ───────────────────────────────────────────


def test_tc1789_the_document_is_exactly_version_one():
    body = vp.utterance_document("0d6c3a0e-1c0a-4a52-9b5e-000000000001", "show games", True)
    assert json.loads(body.decode("utf-8")) == {"v": 1, "request_id": "0d6c3a0e-1c0a-4a52-9b5e-000000000001",
                                               "text": "show games", "wake_checked": True, "source": "hub"}
    assert vp.REQUEST_ID_RE.fullmatch(vp.new_request_id()) and vp.new_request_id() != vp.new_request_id()


@pytest.mark.parametrize("request_id,text,wake_checked,source", [
    ("", "show games", True, "hub"), ("has space", "show games", True, "hub"), ("x" * 65, "show games", True, "hub"),
    ("id-1", "", True, "hub"), ("id-1", "   ", True, "hub"), ("id-1", "x" * 501, True, "hub"),
    ("id-1", "show\ngames", True, "hub"), ("id-1", "show\x7fgames", True, "hub"),
    ("id-1", "show games", "yes", "hub"), ("id-1", "show games", True, "The Hub"),
])
def test_tc1789_what_the_interface_would_refuse_is_never_sent(request_id, text, wake_checked, source):
    with pytest.raises(vp.Invalid) as err:
        vp.utterance_document(request_id, text, wake_checked, source)
    assert "show" not in str(err.value) and "x" * 20 not in str(err.value)


def test_tc1789_token_rule():
    assert vp.token_ok(TOKEN) and vp.token_ok("a" * 32) and vp.token_ok("~" * 256)
    for bad in ("", "a" * 31, "a" * 257, "a" * 31 + " ", "a" * 31 + "\n", "a" * 31 + "é", None, 5):
        assert not vp.token_ok(bad)


# ── TC-1790 the action client against a stand-in of MINI-SERV ────────────────


class StandIn(http.server.BaseHTTPRequestHandler):
    """MINI-SERV's interface as its own code states it (EntertainmentPC
    src/Scenes.Core: ActionRouter's order of checks, ActionWire's reading of a
    request, the replay rule). It records what it was sent."""

    token = TOKEN
    script = []          # statuses to answer before behaving normally
    seen = []            # (method, path, body dict or None, authorization)
    replays = {}
    state = {"v": 1, "state": "ready", "since": "2026-11-11T20:00:00Z", "desktop": "present", "voice": "off",
             "front": None}

    def log_message(self, *args):
        pass

    def _send(self, status, doc):
        raw = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _guard(self):
        if self.headers.get("Authorization") != "Bearer " + self.token:
            self._send(401, {"v": 1, "error": "unauthorized", "detail": "a valid bearer token is required"})
            return False
        return True

    def do_GET(self):
        type(self).seen.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self._guard():
            self._send(200, self.state) if self.path == "/v1/state" else self._send(404, {"v": 1, "error": "not-found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            doc = None
        type(self).seen.append(("POST", self.path, doc, self.headers.get("Authorization")))
        if not self._guard():
            return
        if self.path != "/v1/utterance":
            return self._send(404, {"v": 1, "error": "not-found", "detail": ""})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._send(415, {"v": 1, "error": "unsupported-media-type", "detail": ""})
        if len(raw) > 8192:
            return self._send(413, {"v": 1, "error": "too-large", "detail": ""})
        allowed = {"v", "request_id", "text", "wake_checked", "source"}
        ok = (isinstance(doc, dict) and set(doc) <= allowed and doc.get("v") == 1
              and isinstance(doc.get("request_id"), str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", doc["request_id"])
              and isinstance(doc.get("text"), str) and doc["text"].strip() and len(doc["text"]) <= 500
              and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in doc["text"])
              and isinstance(doc.get("wake_checked"), bool)
              and ("source" not in doc or re.fullmatch(r"[a-z0-9._-]{1,40}", str(doc["source"]))))
        if not ok:
            return self._send(400, {"v": 1, "error": "bad-request", "detail": "the body is not a version 1 request"})
        if type(self).script:
            status = type(self).script.pop(0)
            if status == 503:
                return self._send(503, {"v": 1, "error": "not-ready", "detail": "", "retry_after_seconds": 2})
            if status == 0:
                return self.connection.close()                     # the answer is lost
            return self._send(status, {"v": 1, "error": "x", "detail": ""})
        rid = doc["request_id"]
        if rid in type(self).replays:
            return self._send(200, dict(type(self).replays[rid], replayed=True))
        answer = {"v": 1, "request_id": rid, "outcome": "started", "action": "category:games",
                  "line": "action.started", "detail": ""}
        type(self).replays[rid] = answer
        self._send(200, answer)


@pytest.fixture
def stand_in():
    StandIn.script, StandIn.seen, StandIn.replays = [], [], {}
    server = http.server.HTTPServer(("127.0.0.1", 0), StandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def client(server, token=TOKEN, **over):
    slept = []
    c = vp.ActionClient("127.0.0.1", server.server_address[1], token, sleep=slept.append, **over)
    c.slept = slept
    return c


def test_tc1790_a_transcript_is_posted_with_the_token_and_the_outcome_comes_back(stand_in):
    result = client(stand_in).post_utterance("show games", True)
    assert (result.outcome, result.status, result.attempts, result.replayed) == ("started", 200, 1, False)
    assert result.delivered and result.action_kind == "category"
    method, path, doc, auth = StandIn.seen[0]
    assert (method, path, auth) == ("POST", "/v1/utterance", "Bearer " + TOKEN)
    assert doc["text"] == "show games" and doc["wake_checked"] is True and doc["source"] == "hub" and doc["v"] == 1
    assert "show games" not in repr(result) and TOKEN not in repr(client(stand_in))


def test_tc1790_not_ready_is_tried_again_with_the_same_request_id(stand_in):
    StandIn.script = [503, 503]
    c = client(stand_in)
    result = c.post_utterance("show games", True)
    assert (result.outcome, result.attempts) == ("started", 3) and c.slept == [2.0, 2.0]
    ids = {doc["request_id"] for _m, _p, doc, _a in StandIn.seen}
    assert len(StandIn.seen) == 3 and len(ids) == 1


def test_tc1790_a_lost_answer_is_retried_and_served_from_the_replay(stand_in):
    c = client(stand_in)
    first = c.post_utterance("show games", True, request_id="lost-answer-1")
    again = c.post_utterance("show games", True, request_id="lost-answer-1")
    assert first.outcome == again.outcome == "started" and again.replayed is True and first.replayed is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 415])
def test_tc1790_a_refusal_is_final_and_names_only_its_status(stand_in, status):
    StandIn.script = [status]
    result = client(stand_in).post_utterance("show games", True)
    assert result.outcome == "rejected-%d" % status and result.attempts == 1 and not result.delivered
    assert len(StandIn.seen) == 1


def test_tc1790_a_wrong_token_is_refused_once(stand_in):
    result = client(stand_in, token="w" * 40).post_utterance("show games", True)
    assert result.outcome == "rejected-401" and len(StandIn.seen) == 1


def test_tc1790_nobody_answering_is_dropped_at_the_deadline_never_queued():
    clock = [0.0]

    def refuse_connection(*_a, **_k):
        raise ConnectionRefusedError()

    def sleep(seconds):
        clock[0] += seconds
    c = vp.ActionClient(PEER, 8571, TOKEN, source_ip=HUB, clock=lambda: clock[0], sleep=sleep,
                        connect=refuse_connection, deadline_sec=3.0)
    result = c.post_utterance(SAID, True)
    assert result.outcome == vp.UNDELIVERED_UNREACHABLE and 2 <= result.attempts <= 8 and clock[0] <= 3.0
    assert SAID not in repr(result) and not hasattr(c, "queue")


def test_tc1790_not_ready_until_the_deadline_is_dropped(stand_in):
    StandIn.script = [503] * 10
    clock = [0.0]
    c = vp.ActionClient("127.0.0.1", stand_in.server_address[1], TOKEN, clock=lambda: clock[0],
                        sleep=lambda s: clock.__setitem__(0, clock[0] + s), deadline_sec=5.0)
    result = c.post_utterance("show games", True)
    assert result.outcome == vp.UNDELIVERED_NOT_READY and result.status == 503 and 2 <= result.attempts <= 4


def test_tc1790_text_the_interface_would_refuse_is_not_sent(stand_in):
    assert client(stand_in).post_utterance("x" * 501, True).outcome == vp.NOT_SENT_TEXT and StandIn.seen == []


def test_tc1790_the_connection_is_made_from_the_hub_s_lan_address():
    made = []

    class Conn:
        def __init__(self, host, port, **kwargs):
            made.append((host, port, kwargs))

        def request(self, *a, **k):
            raise OSError()

        def close(self):
            pass
    vp.ActionClient(PEER, 8571, TOKEN, source_ip=HUB, connect=Conn, deadline_sec=0.4,
                    sleep=lambda s: None).post_utterance("show games", True)
    assert made[0][0] == PEER and made[0][1] == 8571 and made[0][2]["source_address"] == (HUB, 0)
    for bad in (("minipc", 8571, TOKEN), (PEER, 0, TOKEN), (PEER, 8571, "short")):
        with pytest.raises(vp.Invalid) as err:
            vp.ActionClient(*bad)
        assert "short" not in str(err.value)


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"v":1,"outcome":"unknown","action":null,"line":null}', ("unknown", False, "", None)),
    (200, b'{"v":1,"outcome":"started","action":"menu:secret title","replayed":true}', ("started", True, "menu", None)),
    (200, b'{"v":1,"outcome":"exploded"}', (vp.ANSWER_NOT_UNDERSTOOD, False, "", None)),
    (200, b'{"v":2,"outcome":"started"}', (vp.ANSWER_NOT_UNDERSTOOD, False, "", None)),
    (200, b"<html>", (vp.ANSWER_NOT_UNDERSTOOD, False, "", None)),
    (503, b'{"v":1,"retry_after_seconds":60}', (vp.UNDELIVERED_NOT_READY, False, "", 5.0)),
    (503, b"", (vp.UNDELIVERED_NOT_READY, False, "", 1.0)),
    (500, b"", ("rejected-500", False, "", None)),
])
def test_tc1790_answers_are_read_as_codes(status, body, expected):
    assert vp.classify_answer(status, body) == expected


# ── TC-1791 MINI-SERV's state ────────────────────────────────────────────────


def test_tc1791_state_is_read_with_the_token(stand_in):
    assert client(stand_in).get_state() == (True, "ready", "off")
    assert StandIn.seen[0][:2] == ("GET", "/v1/state") and StandIn.seen[0][3] == "Bearer " + TOKEN
    assert client(stand_in, token="w" * 40).get_state() == (False, "", "")
    StandIn.state = dict(StandIn.state, voice="armed; rm -rf", state="READY")
    try:
        assert client(stand_in).get_state() == (True, "", "")       # only plain words are taken
    finally:
        StandIn.state = dict(StandIn.state, voice="off", state="ready")


def test_tc1791_one_missed_poll_is_forgiven_two_are_unreachable():
    answers = [(True, "ready", "off"), (False, "", ""), (True, "ready", "armed"), (False, "", ""), (False, "", "")]
    watch = vl.PeerWatch(lambda: answers.pop(0), 5)
    assert watch.current().reachable is False                    # before the first answer: not reachable
    seen = []
    for _ in range(5):
        watch.poll_once()
        seen.append((watch.current().reachable, watch.current().voice))
    assert seen == [(True, "off"), (True, "off"), (True, "armed"), (True, "armed"), (False, "")]


# ── TC-1792 the model pin ────────────────────────────────────────────────────


def test_tc1792_the_committed_manifest_pins_both_models_as_the_windows_listener_does():
    manifest = ve.read_manifest()
    assert set(manifest) == {ve.WAKE_MODEL, ve.STT_MODEL}
    uses = {m: {f["use"] for f in manifest[m]["files"]} for m in manifest}
    assert {"encoder", "decoder", "joiner", "tokens"} <= uses[ve.WAKE_MODEL]
    assert {"encoder", "merged-decoder", "tokens"} <= uses[ve.STT_MODEL]
    for entry in manifest.values():
        assert re.fullmatch(r"[0-9a-f]{64}", entry["archive"]["sha256"]) and entry["archive"]["size"] > 1_000_000
        assert entry["archive"]["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
        for pinned in entry["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", pinned["sha256"]) and pinned["size"] > 0
    # the hub's cost test ran these archives (HomeHub HUB_VOICE_COST_TEST_2026-11-11 section 6)
    assert manifest[ve.WAKE_MODEL]["archive"]["sha256"].startswith("f170013b4716e41b")
    assert manifest[ve.STT_MODEL]["archive"]["sha256"].startswith("9ec31b342d8fa324")
    assert not list(HOME.glob("**/*.onnx")) and not list(HOME.glob("**/*.ort")) and not list(HOME.glob("**/*.wav"))


def small_model(tmp_path):
    import hashlib
    folder = tmp_path / "models" / "m"
    folder.mkdir(parents=True)
    (folder / "enc.onnx").write_bytes(b"encoder bytes")
    (folder / "tokens.txt").write_bytes(b"A 0\n")
    manifest = {"m1": {"id": "m1", "folder": "m", "files": [
        {"use": "encoder", "name": "enc.onnx", "size": 13, "sha256": hashlib.sha256(b"encoder bytes").hexdigest()},
        {"use": "tokens", "name": "tokens.txt", "size": 4, "sha256": hashlib.sha256(b"A 0\n").hexdigest()}]}}
    return str(tmp_path / "models"), manifest, folder


def test_tc1792_only_the_pinned_bytes_load(tmp_path):
    root, manifest, folder = small_model(tmp_path)
    assert set(ve.model_files(root, manifest, "m1", ("encoder", "tokens"))) == {"encoder", "tokens"}
    (folder / "enc.onnx").write_bytes(b"encoder bytez")           # the same size, other bytes
    with pytest.raises(ve.Invalid, match="not the pinned bytes"):
        ve.model_files(root, manifest, "m1", ("encoder",))
    (folder / "enc.onnx").write_bytes(b"encoder bytes and more")
    with pytest.raises(ve.Invalid, match="not the pinned bytes"):
        ve.model_files(root, manifest, "m1", ("encoder",))
    (folder / "enc.onnx").unlink()
    with pytest.raises(ve.Invalid, match="missing"):
        ve.model_files(root, manifest, "m1", ("encoder",))
    with pytest.raises(ve.Invalid, match="no 'joiner' file"):
        ve.model_files(root, manifest, "m1", ("joiner",))
    with pytest.raises(ve.Invalid, match="not in models.manifest.json"):
        ve.model_files(root, manifest, "other", ("encoder",))


def test_tc1792_another_schema_is_refused(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema": "voice-models/2", "models": []}), encoding="utf-8")
    with pytest.raises(ve.Invalid):
        ve.read_manifest(str(path))


# ── TC-1793 the wake phrase as the model's tokens ────────────────────────────

W = ve.WORD_START
KNOWN = [W + "HE", "Y", W + "B", "RU", "N", "O", W + "A", "S"]


def test_tc1793_hey_bruno_becomes_the_model_s_keyword_line():
    assert ve.keyword_line("^HE Y ^B RU N O", "HEY_BRUNO", KNOWN) == W + "HE Y " + W + "B RU N O @HEY_BRUNO"


@pytest.mark.parametrize("tokens,label,known", [
    ("^HE Y ^B RU N X", "HEY_BRUNO", KNOWN),          # a token the model does not have
    ("HE Y ^B RU N O", "HEY_BRUNO", KNOWN),           # no word start in front
    ("^HE", "HEY_BRUNO", KNOWN),                      # one token is not a phrase
    ("^he y", "HEY_BRUNO", KNOWN),                    # tokens are upper case
    ("^HE Y ^B RU N O", "hey bruno", KNOWN),          # the label is a name
    ("^HE Y ^B RU N O", "HEY_BRUNO", KNOWN + ["^X"]),  # the model already uses the stand-in mark
])
def test_tc1793_a_wake_phrase_the_model_cannot_spell_is_refused(tokens, label, known):
    with pytest.raises(ve.Invalid):
        ve.keyword_line(tokens, label, known)


def test_tc1793_tokens_are_read_from_the_model_s_list(tmp_path):
    path = tmp_path / "tokens.txt"
    path.write_text("<blk> 0\n%sHE 1\nY 2\n" % W, encoding="utf-8")
    assert ve.read_tokens(str(path)) == ["<blk>", W + "HE", "Y"]


# ── TC-1794 the reader and the sources ───────────────────────────────────────

CODE = "ABCDEF"


def relay_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text("".join("%s=%s\n" % kv for kv in {
        "LAN_IP": HUB, "CAMERA_LIVINGROOM_RELAY_ENABLED": "true", "CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8564",
        "CAMERA_LIVINGROOM_RELAY_API_PORT": "1994", "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.77",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/h264/ch1/sub/av_stream",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": CODE, "CAMERA_LIVINGROOM_RELAY_AUDIO": "true",
        "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": "768x432"}.items()), encoding="utf-8")
    return str(path)


def test_tc1794_the_reader_asks_for_sound_only_inside_its_stdin_document(tmp_path, monkeypatch):
    import camera_relay as cr
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:" + "0" * 64)
    relay = cr.CameraRelaySettings(relay_env(tmp_path), "livingroom")
    argv, payload = vl.decoder_command(relay, "livingroom")
    line = " ".join(argv)
    assert CODE not in line and "rtsp://" not in line and "192.0.2.77" not in line and HUB not in line
    assert argv[argv.index("--name") + 1] == "homehub-voice-listener-livingroom"
    assert "-vn" in argv and argv[argv.index("-f", argv.index("-vn")) + 1] == "s16le"
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-ac") + 1] == "1" and argv[-1] == "pipe:1"
    assert argv[argv.index("--cpus") + 1] == "0.05" and argv[argv.index("--cpu-shares") + 1] == "2"
    assert "--read-only" in argv and argv[argv.index("--log-driver") + 1] == "none"
    assert b"/livingroom?audio'" in payload and relay.consumer_query == ""          # the caller's settings unchanged
    named, both = vl.decoder_command(relay, "livingroom", "?video&audio", name="homehub-voice-levels-livingroom")
    assert b"/livingroom?video&audio'" in both and named[named.index("--name") + 1] == "homehub-voice-levels-livingroom"
    with pytest.raises(vl.Invalid):
        vl.decoder_command(relay, "livingroom", "?video")
    for forbidden in ("-c", "copy", "-y"):                       # it decodes to a pipe; it never writes a file
        assert forbidden not in argv[argv.index("pipe:0"):]


@pytest.mark.skipif(sys.platform == "win32", reason="the reader's pipe is read without blocking: POSIX only, as on the hub")
def test_tc1794_the_relay_source_reads_its_reader_s_pipe_and_removes_it_on_stop(tmp_path, monkeypatch):
    import subprocess
    import time as real_time
    import camera_relay as cr
    removed, started = [], []
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:" + "0" * 64)
    monkeypatch.setattr(vl.door_relay, "remove_containers", lambda *names, **_k: removed.append(names) or True)

    def fake_consumer(argv, payload, stdout=None, stderr=None):
        started.append((argv, payload))
        child = "import sys, time; sys.stdout.buffer.write(bytes(%d)); sys.stdout.flush(); time.sleep(0.3)"
        return subprocess.Popen([sys.executable, "-c", child % (vg.FRAME_BYTES * 12 + 100)], stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr)
    monkeypatch.setattr(vl.door_relay, "start_consumer", fake_consumer)
    source = vl.RelayAudio(cr.CameraRelaySettings(relay_env(tmp_path), "livingroom"), "livingroom", "?audio",
                           log=lambda *a, **k: None)
    assert source.running is False and source.fileno() is None and source.read_frames(25) == []
    source.start()
    assert source.running and isinstance(source.fileno(), int) and removed == [("homehub-voice-listener-livingroom",)]
    assert b"/livingroom?audio'" in started[0][1]
    frames, ended, deadline = [], False, real_time.time() + 10
    while real_time.time() < deadline and not ended:
        try:
            frames += source.read_frames(5)                      # never blocks: nothing waiting is an empty list
        except EOFError:
            ended = True                                         # the reader ending is told apart from "nothing yet"
        real_time.sleep(0.02)
    assert len(frames) == 12 and all(len(f) == vg.FRAME_BYTES for f in frames) and ended
    assert source.waiting_frames() == 0
    source.stop()
    assert source.running is False and len(removed) == 2 and source.read_frames(25) == []


def test_tc1794_the_frame_queue_gives_whole_frames_and_forgets_on_flush():
    queue = vl.FrameQueue()
    queue.add(tone(200))                                         # two and a half frames
    assert queue.waiting_frames == 2
    assert [len(f) for f in queue.frames(10)] == [vg.FRAME_BYTES] * 2 and queue.waiting_frames == 0
    queue.add(tone(400))
    assert queue.flush() == 5 and queue.frames(10) == [] and len(queue._pending) == 0


def test_tc1794_the_file_source_loops_and_keeps_to_the_clock(tmp_path):
    clock = [100.0]
    source = vl.FileAudio(write_wav(tmp_path / "loop.wav", tone(1000)), clock=lambda: clock[0])
    assert source.read_frames(25) == []                          # not started: nothing
    source.start()
    assert source.read_frames(25) == []                          # no time has passed
    clock[0] += 0.4
    assert len(source.read_frames(25)) == 5
    clock[0] += 10.0                                             # far behind: only a batch, the rest is waiting
    assert len(source.read_frames(25)) == 25 and source.waiting_frames() == 100
    assert source.flush() == 100 and source.waiting_frames() == 0
    clock[0] += 1.6                                              # past the end of the one-second file: it loops
    assert len(source.read_frames(25)) == 20
    source.stop()
    assert source.read_frames(25) == []


# ── TC-1795 the listener loop ────────────────────────────────────────────────


class FakeSource:
    def __init__(self):
        self.running, self.starts, self.stops, self.flushes, self.pending, self.backlog = False, 0, 0, 0, [], 0

    def start(self):
        self.running, self.starts = True, self.starts + 1

    def stop(self):
        self.running, self.stops = False, self.stops + 1
        self.pending = []

    def fileno(self):
        return None

    def read_frames(self, limit):
        assert self.running, "a stopped source must never be read"
        out, self.pending = self.pending[:limit], self.pending[limit:]
        return [bytearray(f) for f in out]

    def waiting_frames(self):
        return self.backlog

    def flush(self):
        dropped, self.flushes, self.backlog, self.pending = self.backlog + len(self.pending), self.flushes + 1, 0, []
        return dropped


class Rig:
    """A Listener on stand-ins, with the inputs to the arm decision in hand."""

    def __init__(self, tmp_path=None, **over):
        self.hold, self.peer, self.presence = False, READY, HERE_NOW
        self.now, self.wall, self.reports, self.logs = [0], [1_800_000_000.0], [], []
        self.wake, self.stt, self.posts, self.source = FakeWake(), FakeStt(), Posts(), FakeSource()
        self.s = settings(**over)
        self.pipeline = vl.new_pipeline(self.s, self.wake, self.stt, self.posts)
        self.listener = vl.Listener(self.s, self.pipeline, self.source,
                                    lambda: (self.hold, self.peer, self.presence), self.reports.append,
                                    lambda: self.now[0], wall=lambda: self.wall[0],
                                    log=lambda m, warning=False: self.logs.append(m))

    def step(self, ms=vg.FRAME_MS, audio=b""):
        self.now[0] += ms
        self.source.pending += frames_of(audio)
        return self.listener.step()

    def play(self, audio):
        events = []
        for frame in frames_of(audio):
            events += self.step(vg.FRAME_MS, frame)
        return events


def test_tc1795_disarmed_it_never_starts_or_reads_the_source():
    rig = Rig()
    rig.presence = EMPTY
    for _ in range(50):
        rig.step()
    assert rig.source.starts == 0 and rig.listener.armed is False
    assert rig.reports[-1]["state"] == "disarmed" and rig.reports[-1]["detail"] == vg.DISARM_ROOM_EMPTY
    rig.peer = vg.PeerState(True, "ready", "muted")
    rig.step(vl.ARM_EVERY_MS)
    assert rig.reports[-1]["detail"] == vg.DISARM_PEER_MUTED and rig.source.starts == 0


def test_tc1795_it_asks_whether_it_may_hear_twice_a_second_not_on_every_pass():
    rig = Rig()
    asked = []
    inputs = rig.listener.arm_inputs
    rig.listener.arm_inputs = lambda: asked.append(rig.now[0]) or inputs()
    rig.play(quiet(4000))
    assert 8 <= len(asked) <= 9 and vl.BATCH_SEC * 1000 < vl.ARM_EVERY_MS


def test_tc1795_arming_opens_the_source_and_disarming_closes_and_wipes():
    rig = Rig()
    rig.play(quiet(2000))
    assert rig.source.starts == 1 and rig.listener.armed and rig.reports[-1]["state"] == "armed"
    assert not rig.pipeline.holds_no_audio                       # armed and quiet: the gate's pre-roll is held
    rig.presence = EMPTY
    rig.step(vl.ARM_EVERY_MS)                                    # within half a second
    assert rig.source.stops == 1 and rig.source.running is False and rig.pipeline.holds_no_audio
    assert rig.reports[-1]["state"] == "disarmed" and rig.listener.counts == dict(rig.listener.counts, arms=1, disarms=1)
    rig.hold = True
    rig.step(vl.ARM_EVERY_MS)
    assert rig.reports[-1]["detail"] == vg.DISARM_HOLD and rig.source.starts == 1
    assert any("armed - the microphone is being read" in m for m in rig.logs)
    assert any("disarmed (room-empty) - nothing is being read" in m for m in rig.logs)


def test_tc1795_a_request_runs_through_and_what_queued_up_meanwhile_is_dropped():
    rig = Rig(HOLD_AFTER_POST_MS="0")
    rig.play(REQUEST[:len(REQUEST) - vg.bytes_for(1200)])      # the request is still open
    assert rig.posts == []
    rig.source.backlog = 7
    rig.play(quiet(800))
    assert rig.posts == [(SAID, True)] and rig.source.flushes >= 1 and rig.listener.counts["skipped_frames"] >= 7
    shown = [r["state"] for r in rig.reports]
    assert shown[0] == "armed" and shown[-1] == "armed"
    assert [s for s in shown if s != "armed"][:3] == ["hearing", "listening", "working"]     # each as it happens


def test_tc1795_falling_behind_live_drops_the_backlog_rather_than_hearing_it_late():
    rig = Rig()
    rig.play(quiet(400))
    rig.source.backlog = vl.MAX_FRAMES_PER_STEP + 1
    rig.step(audio=quiet(80))
    assert rig.source.flushes == 1 and rig.listener.counts["skipped_frames"] == vl.MAX_FRAMES_PER_STEP + 1


def test_tc1795_a_silent_reader_is_restarted_and_three_times_ends_the_run():
    rig = Rig(STALL_SEC="5")
    rig.play(quiet(400))
    for _ in range(3):
        assert rig.listener.fatal is None
        rig.step(5100)
    assert rig.listener.counts["stalls"] == 3 and rig.source.starts == 3 and "stalled 3 times" in rig.listener.fatal
    assert rig.pipeline.holds_no_audio


def test_tc1795_at_its_end_date_it_stops_hearing_and_the_run_ends():
    rig = Rig(UNTIL="2027-01-15T08:00:10Z")
    rig.play(quiet(800))
    assert rig.listener.armed and rig.source.running and not rig.listener.ended
    rig.wall[0] += 11
    rig.step()
    assert rig.listener.ended and not rig.listener.armed and rig.source.running is False
    assert rig.pipeline.holds_no_audio and rig.reports[-1]["detail"] == "ended" and rig.listener.fatal is None
    rig.step(audio=tone(800))
    assert rig.source.starts == 1                                # nothing starts it again
    rig.listener.close()
    assert rig.reports[-1]["detail"] == "ended"


def test_tc1795_engine_faults_in_a_row_end_the_run():
    rig = Rig()
    rig.wake.fail_after = 0
    for _ in range(vl.ENGINE_ERRORS_BEFORE_EXIT + 1):
        rig.play(quiet(800) + tone(800, -20.0))
    assert "an engine failed" in (rig.listener.fatal or "")


# ── TC-1796 nothing that was said reaches the journal, the status or the feed ──


def test_tc1796_the_transcript_goes_to_the_post_and_nowhere_else(tmp_path):
    rig = Rig(HOLD_AFTER_POST_MS="0")
    rig.play(REQUEST)
    assert rig.posts == [(SAID, True)]
    status = rig.listener.status_record()
    vl.write_status(str(tmp_path), "livingroom", status)
    on_disk = (tmp_path / "livingroom" / "status.json").read_text(encoding="utf-8")
    everything = json.dumps([rig.reports, rig.logs, status]) + on_disk
    for word in SAID.split():
        assert word not in everything.replace("the microphone", "").replace("the wake phrase", "") \
            .replace("nothing", "").replace("the request", "")
    assert set(status) == {"camera", "source", "state", "detail", "since", "armed", "armed_sec", "counts", "outcomes",
                           "gate", "engine_sec", "duty_refused_sec", "background_dbfs", "holds_no_audio"}
    assert status["outcomes"] == {"started": 1} and status["counts"]["wake_fires"] == 1
    assert any("a request was sent to MINI-SERV: started" in m for m in rig.logs)
    assert any("the wake phrase was heard" in m for m in rig.logs)


def test_tc1796_the_feed_record_is_a_state_a_reason_and_a_time():
    rig = Rig()
    rig.presence = EMPTY
    rig.step()
    record = rig.reports[-1]
    assert record == {"v": 1, "type": "state", "event": "living_room.listener", "state": "disarmed",
                      "detail": "room-empty", "since": "2027-01-15T08:00:00Z"}
    import event_feed
    assert len(event_feed.encode(record)) <= event_feed.MAX_LINE
    rig.listener.close()
    assert rig.reports[-1]["detail"] == "stopped"


# ── TC-1797 the unit and the template ────────────────────────────────────────


def unit_text():
    return (HOME / "homehub-voice-listener@.service").read_text(encoding="utf-8")


def test_tc1797_the_unit_is_knob_gated_low_priority_and_keeps_nothing():
    unit = unit_text()
    assert "ExecCondition=/usr/bin/python3 /opt/homehub/stack/voice-listener/voice_listener.py --camera %i condition" in unit
    assert "ExecStart=/opt/homehub-voice/venv/bin/python /opt/homehub/stack/voice-listener/voice_listener.py --camera %i run" in unit
    assert "AssertPathExists=/opt/homehub-voice/venv/bin/python" in unit
    assert "RestartPreventExitStatus=78" in unit and "Restart=on-failure" in unit
    assert "Wants=docker.service network-online.target homehub-camera-relay@%i.service" in unit
    assert not re.search(r"^Requires=", unit, re.M)
    for line in ("Nice=19", "CPUWeight=1", "CPUQuota=100%", "MemoryMax=512M", "MemorySwapMax=0", "LimitCORE=0",
                 "NoNewPrivileges=true", "PrivateTmp=true", "ProtectHome=true", "StateDirectory=homehub-voice",
                 "RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK", "IPAddressDeny=any",
                 "IPAddressAllow=localhost 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16",
                 "RuntimeDirectory=homehub-voice", "ExecStopPost=-/usr/bin/docker rm -f homehub-voice-listener-%i"):
        assert re.search("^" + re.escape(line) + "$", unit, re.M), line
    assert "StandardOutput" not in unit and "StandardError" not in unit      # the journal, by sd-daemon prefixes


def env_example(key):
    for line in (STACK / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip('"')
    raise AssertionError(key + " is not in .env.example")


def test_tc1797_the_template_ships_it_off_with_no_source_and_no_ruling():
    assert env_example("CAMERA_LIVINGROOM_LISTENER_ENABLED") == "false"
    assert env_example("CAMERA_LIVINGROOM_LISTENER_SOURCE") == "none"
    assert env_example("CAMERA_LIVINGROOM_LISTENER_ROOM_AUDIO_RULING") == ""
    assert env_example("CAMERA_LIVINGROOM_LISTENER_UNTIL") == ""
    assert env_example("CAMERA_LIVINGROOM_LISTENER_PRESENCE") == "required"
    assert env_example("CAMERA_LIVINGROOM_LISTENER_FEED_PORT") == "0"
    assert env_example("MINISERV_ACTION_PORT") == "0" and env_example("MINISERV_ACTION_TOKEN") == ""
    template = {}
    for line in (STACK / ".env.example").read_text(encoding="utf-8").splitlines():
        if re.match(r"(CAMERA_LIVINGROOM_LISTENER_|MINISERV_ACTION_|LAN_IP=|MAIN_BOX_IP=)", line):
            key, _, value = line.partition("=")
            template[key] = value.strip('"')
    s = vl.ListenerSettings(template, "livingroom")
    assert s.enabled is False and s.hears_room is False
    assert vl.refusals(s, template) == ["CAMERA_LIVINGROOM_LISTENER_SOURCE is none: no audio source is configured"]
    documented = {k for k in template if k.startswith("CAMERA_LIVINGROOM_LISTENER_")}
    read = set(re.findall(r'"([A-Z][A-Z_]+)"', (HOME / "voice_listener.py").read_text(encoding="utf-8")))
    for key in documented:
        assert key[len("CAMERA_LIVINGROOM_LISTENER_"):] in read, key + " is in the template but read by nothing"


# ── TC-1798 the runtime lock and the installer ───────────────────────────────


def test_tc1798_the_lock_is_three_exact_hashed_wheels():
    lines = [ln for ln in (HOME / "requirements.lock").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    pins = dict(re.fullmatch(r"([a-z0-9-]+)==([0-9.]+) --hash=sha256:[0-9a-f]{64}", ln).groups() for ln in lines)
    assert pins == {"numpy": "1.26.4", "sherpa-onnx-core": "2.0.1", "sherpa-onnx": "2.0.1"}
    assert pins["sherpa-onnx"] == ve.ENGINE_VERSION
    presence_lock = (STACK / "autoinstall" / "wall" / "sensor-wheelhouse" / "requirements.lock").read_text(encoding="utf-8")
    numpy_hash = re.search(r"numpy==1.26.4 --hash=sha256:([0-9a-f]{64})", "\n".join(lines)).group(1)
    assert numpy_hash in presence_lock            # one numpy on the hub, byte for byte


def test_tc1798_the_installer_takes_nothing_from_a_network_and_checks_every_byte():
    script = (HOME / "install-voice-runtime.sh").read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env bash\n") and "\r" not in script and "set -euo pipefail" in script
    assert "--no-index" in script and "--only-binary=:all:" in script and "--require-hashes" in script
    assert "requirements.lock" in script and "models.manifest.json" in script
    assert "hashlib.sha256" in script and 'model["archive"]["sha256"]' in script and 'pinned["sha256"]' in script
    assert "tar.extractall" not in script and "tar -x" not in script      # no path from an archive is followed
    for forbidden in ("curl ", "wget ", "pip download", "https://", "http://"):
        assert forbidden not in script
    assert "\r" not in (HOME / "requirements.lock").read_text(encoding="utf-8")


# ── TC-1799 no code path writes sound ────────────────────────────────────────


def test_tc1799_the_only_files_written_are_configuration_and_state():
    writers = {}
    for path in sorted(HOME.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "\r" not in text
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"""open\([^)]*["'](w|wb|a|ab|x|r\+)["']|\.tofile\(|wave\.open\([^)]*["']wb["']|os\.fdopen\(""", line):
                writers.setdefault(path.name, []).append(line.strip())
        for forbidden in ("pickle", "shelve", "sqlite3", "logging", "soundfile", "sounddevice", "tempfile.NamedTemporaryFile"):
            assert not re.search(r"^\s*(import|from)\s+" + re.escape(forbidden.split(".")[0]) + r"\b", text, re.M) \
                or forbidden.startswith("tempfile"), "%s imports %s" % (path.name, forbidden)
    assert set(writers) == {"voice_engines.py", "voice_listener.py"}
    assert writers["voice_engines.py"] == ['with open(keywords_path, "w", encoding="utf-8", newline="\\n") as handle:']
    assert sorted(writers["voice_listener.py"]) == sorted([
        'with open(tmp, "w", encoding="utf-8") as handle:',                       # the status file
        'with os.fdopen(fd, "w", encoding="utf-8", newline="\\n") as handle:'])   # the token into .env
    session = (HOME / "voice_session.py").read_text(encoding="utf-8")
    post = (HOME / "voice_post.py").read_text(encoding="utf-8")
    for name, text in (("voice_session.py", session), ("voice_post.py", post),
                       ("voice_gate.py", (HOME / "voice_gate.py").read_text(encoding="utf-8"))):
        assert not re.search(r"(print|open|log)\(", text), name + " prints, logs or opens a file"


def test_tc1799_no_message_is_built_from_a_transcript():
    listener = (HOME / "voice_listener.py").read_text(encoding="utf-8")
    for line in listener.splitlines():
        if "mc.log(" in line or "self.log(" in line or "door_relay.emit(" in line:
            assert not re.search(r"\b(text|heard\[|transcript)\b", line.split("#")[0]), line


# ── TC-1800 the token's way into .env ────────────────────────────────────────


def test_tc1800_the_token_is_written_from_stdin_and_never_shown(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LAN_IP=%s\n%s=old\n" % (HUB, vl.TOKEN_KEY), encoding="utf-8")
    assert vl.import_token(str(path), (TOKEN + "\n").encode("ascii")) == vl.TOKEN_KEY
    assert path.read_text(encoding="utf-8") == "LAN_IP=%s\n%s=%s\n" % (HUB, vl.TOKEN_KEY, TOKEN)
    assert vl.load(str(path), "livingroom").token == TOKEN
    for bad in (b"short\n", b"a" * 40 + b" b\n", b"", b"a" * 2000, ("a" * 31 + "é").encode("utf-8")):
        with pytest.raises(vl.Invalid) as err:
            vl.import_token(str(path), bad)
        assert "aaaa" not in str(err.value)
    assert TOKEN in path.read_text(encoding="utf-8")             # a refusal leaves the file as it was


# ── TC-1807 the room as numbers ──────────────────────────────────────────────


def test_tc1807_levels_reads_the_room_as_numbers_only_and_behind_the_same_locks(tmp_path, monkeypatch, capsys):
    made = []

    class FakeRelayAudio(FakeSource):
        def __init__(self, relay, camera, query, name=None):
            super().__init__()
            self.query, self.name = query, name
            self.pending = frames_of(quiet(2000) + tone(800, -20.0) + quiet(2400))
            made.append(self)
    monkeypatch.setattr(vl, "RelayAudio", FakeRelayAudio)
    monkeypatch.setattr(vl.mc, "log", lambda *a, **k: None)
    relay_keys = open(relay_env(tmp_path), encoding="utf-8").read()

    def env_file(**over):
        path = tmp_path / "levels.env"
        lines = ["CAMERA_LIVINGROOM_LISTENER_%s=%s" % kv for kv in over.items()]
        path.write_text(relay_keys + "\n".join(lines + [""]), encoding="utf-8")
        return str(path)
    clock = [0.0]
    run = lambda path: vl.levels(path, "livingroom", 10, sleep=lambda s: clock.__setitem__(0, clock[0] + s),  # noqa: E731
                                 clock=lambda: clock[0])
    assert run(env_file()) == vl.EXIT_REFUSED                       # as shipped: no source
    assert run(env_file(SOURCE="relay")) == vl.EXIT_REFUSED         # the room's sound needs the ruling
    assert made == []                                               # and no reader was built for either
    assert run(env_file(SOURCE="relay", ROOM_AUDIO_RULING="ratified-2026-12-01")) == 0      # no MINI-SERV needed
    got = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(got) == {"camera", "seconds_heard", "level_dbfs", "background_dbfs", "gate_opens", "gate_open_pct"}
    assert got["seconds_heard"] == 5.2 and got["gate_opens"] == 1 and 20 < got["gate_open_pct"] < 60
    assert got["level_dbfs"]["p10"] < -65 and got["level_dbfs"]["p99"] > -21 and got["background_dbfs"] < -55
    assert made[0].starts == 1 and made[0].stops == 1 and made[0].query == "?audio"
    assert made[0].name == "homehub-voice-levels-livingroom"         # never the running listener's own reader
    assert vl.percentile([], 50) is None and vl.percentile([3.0, 1.0, 2.0], 50) == 2.0


# ── TC-1803 the cost verdict ─────────────────────────────────────────────────


def test_tc1803_cost_is_reader_plus_listener_against_the_armed_budget(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vl, "unit_cgroup", lambda camera: "/sys/fs/cgroup/unit")
    monkeypatch.setattr(vl.door_relay, "container_cgroup", lambda name: "/sys/fs/cgroup/" + name)
    seen = {}

    def fake_cost(groups, seconds):
        seen.update(groups=groups, seconds=seconds)
        return {"detector": {"cpu_pct_one_core": 15.9}, "decoder": {"cpu_pct_one_core": 0.8},
                "relay": {"cpu_pct_one_core": 2.1}, "psi_some_avg60_mean": 9.4, "seconds": seconds}
    monkeypatch.setattr(vl.door_relay, "cost", fake_cost)
    assert vl.cost(write_env(tmp_path), "livingroom", 1800) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["total_cpu_pct_one_core"] == 16.7 and result["budget_pct"] == 20 and result["pass"] is True
    assert seen["groups"]["decoder"].endswith("homehub-voice-listener-livingroom") and seen["seconds"] == 1800
    assert vl.cost(write_env(tmp_path, BUDGET_ARMED_PCT="10"), "livingroom", 60) == 1
    monkeypatch.setattr(vl.door_relay, "cost", lambda g, s: {"detector": {"cpu_pct_one_core": 1.0},
                                                             "decoder": {"missing": "x"}})
    assert vl.cost(write_env(tmp_path), "livingroom", 60) == 1      # not hearing is not a pass
