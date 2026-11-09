"""Room presence from a relayed camera (SN-055, SR-157..SR-160).

What runs here, on the dev PC: the zones and their refusals, the settings, the
model pin, the YOLO decode and letterbox geometry, the anchors, the state
machine against the plan's targets (entry, a still person held, absence within
the hold, the treadmill never counting, motion never starting presence), the
engage signal, the face-first cascade, the feed (admitted peers, state on
connect, nothing but state on the wire), the decoder's argv, and the
unit/template contract.

What does not: onnxruntime, the models and the camera. The live proof is the
plan's section 6, walked by the operator with no recording.
Implements: TC-1736..TC-1747.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
import time
from pathlib import Path

import pytest

try:
    import numpy as np
except ImportError:  # the WSL runner has none; the dev PC's and the hub's do
    np = None
needs_numpy = pytest.mark.skipif(np is None, reason="numpy is not installed")

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-presence", "camera-relay", "camera-motion", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_presence as cp  # noqa: E402
import door_relay as dr  # noqa: E402

ROOM = "0.05,0.2;0.95,0.2;0.95,0.95;0.05,0.95"
TREADMILL = "0.70,0.2;0.95,0.2;0.95,0.95;0.70,0.95"
COUCH = "0.05,0.5;0.45,0.5;0.45,0.95;0.05,0.95"


def env(**over):
    base = {"MAIN_BOX_IP": "192.0.2.50", "CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON": ROOM,
            "CAMERA_LIVINGROOM_PRESENCE_EXCLUDE_POLYGONS": TREADMILL}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def settings(**over):
    return cp.PresenceSettings(env(**over), "livingroom")


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1736 zones ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "0,0;1,1", "0,0;1,0;2,1", "a,b;1,0;1,1", "0,0;1,0;1,-1"])
def test_tc1736_bad_polygons_are_refused_by_key(text):
    with pytest.raises(cp.Invalid) as err:
        cp.parse_polygon(text, "K")
    assert "K" in str(err.value)


def test_tc1736_the_treadmill_never_counts_and_the_room_does():
    s = settings()
    assert s.zones.counts((0.3, 0.6))          # the middle of the room
    assert not s.zones.counts((0.8, 0.6))      # on the treadmill
    assert not s.zones.counts((0.5, 0.1))      # above the room (the ceiling, a wall)
    assert cp.parse_polygons("", "K") == [] and len(cp.parse_polygons(TREADMILL + "|" + COUCH, "K")) == 2


def test_tc1736_the_crop_is_the_rooms_padded_bounding_box():
    x0, y0, x1, y1 = settings().zones.crop()
    assert (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)) == (0.01, 0.16, 0.99, 0.99)


@needs_numpy
def test_tc1736_the_motion_mask_excludes_the_treadmill():
    mask = settings().zones.motion_mask(cp.MOTION_W, cp.MOTION_H)
    assert mask[int(0.6 * cp.MOTION_H), int(0.3 * cp.MOTION_W)]
    assert not mask[int(0.6 * cp.MOTION_H), int(0.85 * cp.MOTION_W)]
    assert not mask[2, 2]


# ── TC-1737 settings ─────────────────────────────────────────────────────────


def test_tc1737_defaults_and_the_feed_peer_is_mini_serv():
    s = settings()
    assert (s.enabled, s.face_model, s.body_model, s.body_mode) == (False, "faceyolo_320.onnx", "yolov8n_256.onnx",
                                                                    "cascade")
    assert (s.face_conf, s.body_conf, s.confirm_count, s.confirm_window, s.hold_sec) == (0.15, 0.40, 2, 3, 8)
    assert s.feed_port == 0 and s.feed_peers == ["192.0.2.50"] and not s.placeholder


def test_tc1737_the_whole_frame_with_nothing_excluded_is_the_placeholder():
    assert settings(CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON="0,0;1,0;1,1;0,1",
                    CAMERA_LIVINGROOM_PRESENCE_EXCLUDE_POLYGONS=None).placeholder


@pytest.mark.parametrize("over, key", [
    ({"CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON": None}, "ROOM_POLYGON"),
    ({"CAMERA_LIVINGROOM_PRESENCE_BODY_MODE": "sometimes"}, "BODY_MODE"),
    ({"CAMERA_LIVINGROOM_PRESENCE_FACE_CONF": "1.5"}, "FACE_CONF"),
    ({"CAMERA_LIVINGROOM_PRESENCE_FACE_MODEL": "../x.onnx"}, "FACE_MODEL"),
    ({"CAMERA_LIVINGROOM_PRESENCE_CONFIRM_COUNT": "4"}, "CONFIRM_COUNT"),
    ({"CAMERA_LIVINGROOM_PRESENCE_FEED_PORT": "8566", "MAIN_BOX_IP": None}, "FEED_PEERS"),
])
def test_tc1737_refusals_name_the_key(over, key):
    with pytest.raises(cp.Invalid) as err:
        settings(**over)
    assert key in str(err.value)


# ── TC-1738 the model pin ────────────────────────────────────────────────────


def test_tc1738_only_the_pinned_bytes_load(tmp_path):
    (tmp_path / "m.onnx").write_bytes(b"model")
    manifest = {"m.onnx": {"sha256": hashlib.sha256(b"model").hexdigest()}}
    assert cp.verify_model(str(tmp_path), "m.onnx", manifest).endswith("m.onnx")
    (tmp_path / "m.onnx").write_bytes(b"other")
    for name in ("m.onnx", "absent.onnx", "unlisted.onnx"):
        with pytest.raises(cp.Invalid):
            cp.verify_model(str(tmp_path), name, dict(manifest, **{"absent.onnx": {"sha256": "0" * 64}}))


def test_tc1738_the_manifest_pins_every_default_model():
    manifest = cp.read_manifest()
    s = settings()
    for name in (s.face_model, s.body_model):
        assert re.fullmatch(r"[0-9a-f]{64}", manifest[name]["sha256"])


# ── TC-1739 decode, letterbox and anchors ────────────────────────────────────


@needs_numpy
def test_tc1739_yolov8_decode_takes_one_class_above_threshold_in_either_layout():
    # Anchors always outnumber channels (756 at 192 input); the decoder relies on it.
    out = np.zeros((1, 5, 12), dtype=np.float32)
    out[0, :, 1] = [100, 50, 20, 30, 0.6]
    out[0, :, 2] = [10, 10, 5, 5, 0.1]
    boxes = cp.decode_yolo(out, 0.15)
    assert len(boxes) == 1 and boxes[0][1:] == (100.0, 50.0, 20.0, 30.0)
    assert boxes[0][0] == pytest.approx(0.6)
    assert len(cp.decode_yolo(out[0].T, 0.15)) == 1           # (anchors, 4+C)
    coco = np.zeros((1, 84, 120), dtype=np.float32)
    coco[0, 4 + 56, 0] = 0.9                                   # a chair, not a person
    assert cp.decode_yolo(coco, 0.4, cp.COCO_PERSON) == []


@needs_numpy
def test_tc1739_letterbox_is_top_left_zero_padded_and_scaled():
    rgb = np.full((360, 640, 3), 255, dtype=np.uint8)
    resize = lambda a, size: np.full((size[1], size[0], 3), 255, dtype=np.uint8)  # noqa: E731
    blob, scale = cp.letterbox(rgb, 320, resize=resize)
    assert blob.shape == (1, 3, 320, 320) and scale == pytest.approx(0.5)
    assert blob[0, 0, 179, 0] == pytest.approx(1.0) and blob[0, 0, 181, 0] == 0.0


def test_tc1739_anchors_are_a_faces_centre_and_a_bodys_feet_on_the_whole_frame():
    crop = (64, 72, 640, 360)                                   # crop origin in frame pixels
    face = cp.to_frame([(0.5, 100.0, 50.0, 20.0, 30.0)], "face", 0.5, crop, 640, 360)
    body = cp.to_frame([(0.5, 100.0, 50.0, 20.0, 30.0)], "body", 0.5, crop, 640, 360)
    assert face[0][2] == pytest.approx(((64 + 200) / 640, (72 + 100) / 360))
    assert body[0][2] == pytest.approx(((64 + 200) / 640, (72 + 130) / 360))


# ── TC-1740 presence: entry, stillness, absence, the treadmill, motion ──────


ROOM_HIT = [("face", 0.5, (0.3, 0.6))]
TREADMILL_HIT = [("body", 0.9, (0.8, 0.9)), ("face", 0.9, (0.82, 0.4))]


def run(state, seconds, evidence, motion=0.0, start=0.0):
    """Feed one sample a second; evidence(t) -> detections. Returns (t, record) events."""
    events = []
    for t in range(int(seconds)):
        mono = start + t
        for record in state.update(mono, 1_700_000_000 + mono, evidence(mono), motion):
            events.append((mono, record))
    return events


def test_tc1740_entry_needs_two_positive_seconds_of_three():
    state = cp.PresenceState(settings())
    assert run(state, 1, lambda t: ROOM_HIT) == []
    events = run(state, 1, lambda t: ROOM_HIT, start=1)
    assert len(events) == 1 and events[0][1]["present"] is True
    assert events[0][1]["event"] == "living_room.presence" and events[0][1]["zone"] == "room"


def test_tc1740_a_single_flicker_never_enters():
    state = cp.PresenceState(settings())
    assert run(state, 30, lambda t: ROOM_HIT if t % 4 == 0 else []) == []


def test_tc1740_a_still_person_is_held_by_sparse_positives_for_minutes():
    """Sitting still on the couch for 3+ minutes: the detector scores the person
    only every fifth second (looking away), and presence must never drop."""
    state = cp.PresenceState(settings())
    run(state, 2, lambda t: ROOM_HIT)
    events = run(state, 240, lambda t: ROOM_HIT if t % 5 == 0 else [], start=2)
    assert events == []
    assert state.present


def test_tc1740_absence_lands_within_the_hold_of_the_last_positive():
    state = cp.PresenceState(settings())
    run(state, 10, lambda t: ROOM_HIT)
    events = run(state, 20, lambda t: [], start=10)
    assert len(events) == 1 and events[0][1]["present"] is False
    assert events[0][0] - 9 <= 8 + 1        # the plan: absence within about ten seconds


def test_tc1740_the_treadmill_never_makes_presence():
    state = cp.PresenceState(settings())
    assert run(state, 120, lambda t: TREADMILL_HIT, motion=0.5) == []


def test_tc1740_motion_holds_a_present_person_but_never_starts_presence():
    state = cp.PresenceState(settings())
    assert run(state, 30, lambda t: [], motion=0.5) == []
    run(state, 3, lambda t: ROOM_HIT, start=30)
    assert run(state, 60, lambda t: [], motion=0.5, start=33) == []
    off = cp.PresenceState(settings(CAMERA_LIVINGROOM_PRESENCE_MOTION_HOLDS="false"))
    run(off, 3, lambda t: ROOM_HIT)
    assert len(run(off, 20, lambda t: [], motion=0.5, start=3)) == 1


def test_tc1740_confidence_is_the_best_score_in_the_hold_and_zero_when_absent():
    state = cp.PresenceState(settings())
    run(state, 3, lambda t: [("body", 0.45, (0.3, 0.6)), ("face", 0.62, (0.3, 0.5))])
    assert state.presence_record(3)["confidence"] == 0.62
    run(state, 20, lambda t: [], start=3)
    assert state.presence_record(23)["confidence"] == 0.0


# ── TC-1741 engage ───────────────────────────────────────────────────────────


def test_tc1741_engage_after_the_couch_is_held_and_off_after_the_hold():
    state = cp.PresenceState(settings(CAMERA_LIVINGROOM_PRESENCE_ENGAGE_POLYGON=COUCH))
    events = run(state, 40, lambda t: ROOM_HIT)
    engaged = [(t, r) for t, r in events if r["event"] == "living_room.engage"]
    assert engaged and engaged[0][1]["engaged"] is True and engaged[0][0] == 20
    off = run(state, 20, lambda t: [], start=40)
    assert [r["engaged"] for _t, r in off if r["event"] == "living_room.engage"] == [False]


def test_tc1741_someone_elsewhere_in_the_room_never_engages():
    state = cp.PresenceState(settings(CAMERA_LIVINGROOM_PRESENCE_ENGAGE_POLYGON=COUCH))
    events = run(state, 60, lambda t: [("face", 0.5, (0.6, 0.4))])
    assert [r["event"] for _t, r in events] == ["living_room.presence"]


def test_tc1741_without_an_engage_zone_there_are_no_engage_records():
    state = cp.PresenceState(settings())
    assert all(r["event"] == "living_room.presence" for _t, r in run(state, 60, lambda t: ROOM_HIT))
    assert len(state.snapshot(0)) == 1


# ── TC-1742 the cascade ──────────────────────────────────────────────────────


class FakeDetector:
    def __init__(self, boxes):
        self.boxes, self.calls = boxes, 0

    def detect(self, crop):
        self.calls += 1
        return self.boxes, 1.0


@needs_numpy
def test_tc1742_body_runs_only_when_no_face_counted():
    s = settings(CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON="0,0;1,0;1,1;0,1")
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    face_in_room = FakeDetector([(0.5, 200.0, 200.0, 10.0, 10.0)])
    body = FakeDetector([])
    cp.Evaluator(s, face_in_room, body).evaluate(frame)
    assert body.calls == 0
    face_on_treadmill = FakeDetector([(0.5, 600.0, 200.0, 10.0, 10.0)])
    cp.Evaluator(s, face_on_treadmill, body).evaluate(frame)
    assert body.calls == 1
    always = settings(CAMERA_LIVINGROOM_PRESENCE_BODY_MODE="always",
                      CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON="0,0;1,0;1,1;0,1")
    cp.Evaluator(always, face_in_room, body).evaluate(frame)
    assert body.calls == 2


# ── TC-1743 the feed ─────────────────────────────────────────────────────────


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pump(f, rounds=20):
    import select
    for _ in range(rounds):
        r, w, _x = select.select(f.read_fds(), f.write_fds(), [], 0.05)
        f.service(set(r), set(w))


def read_lines(sock, want, timeout=2.0):
    sock.settimeout(timeout)
    data = b""
    end = time.time() + timeout
    while data.count(b"\n") < want and time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    return [json.loads(l) for l in data.splitlines()]


def test_tc1743_an_admitted_peer_gets_hello_then_the_current_state_then_changes():
    state = cp.PresenceState(settings(CAMERA_LIVINGROOM_PRESENCE_ENGAGE_POLYGON=COUCH))
    f = cp.PresenceFeed("livingroom", "127.0.0.1", free_port(), ["127.0.0.1"])
    f.current = state.snapshot(0)
    assert f.open()
    client = socket.create_connection(("127.0.0.1", f.port))
    try:
        pump(f)
        hello, presence, engage = read_lines(client, 3)
        assert hello["type"] == "hello" and presence["present"] is False and engage["engaged"] is False
        for _t, record in run(state, 3, lambda t: ROOM_HIT):
            f.publish_state(record)
        pump(f)
        (change,) = read_lines(client, 1)
        assert change["event"] == "living_room.presence" and change["present"] is True
        for line in (hello, presence, engage, change):
            assert not {"polygon", "box", "boxes", "image", "frame", "anchor"} & set(line)
            assert len(json.dumps(line, separators=(",", ":"))) < 256
    finally:
        client.close()
        f.close()


def test_tc1743_a_peer_that_is_not_admitted_is_closed():
    f = cp.PresenceFeed("livingroom", "127.0.0.1", free_port(), ["192.0.2.50"])
    f.current = [{"v": 1, "type": "state"}]
    assert f.open()
    client = socket.create_connection(("127.0.0.1", f.port))
    try:
        pump(f)
        client.settimeout(1.0)
        assert client.recv(100) == b""
        assert f.clients == []
    finally:
        client.close()
        f.close()


# ── TC-1744 the decoder ──────────────────────────────────────────────────────


def test_tc1744_the_decoder_is_video_only_one_fps_and_holds_no_credential(tmp_path):
    import camera_relay as cr
    path = tmp_path / "env"
    path.write_text("LAN_IP=192.0.2.10\nCAMERA_LIVINGROOM_RELAY_RTSP_PORT=8564\nCAMERA_LIVINGROOM_RELAY_API_PORT=1994\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST=192.0.2.69\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH=/h264/ch1/sub/av_stream\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD=ABCDEF\n"
                    "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE=768x432\n")
    relay = cr.CameraRelaySettings(str(path), "livingroom")
    argv, payload = cp.decoder_command(relay, "livingroom")
    joined = "\n".join(argv)
    assert "ABCDEF" not in joined and "rtsp://" not in joined and "192.0.2.69" not in joined
    assert "-an" in argv and any(a.startswith("fps=1,scale=640:360") for a in argv)
    assert argv[argv.index("--cpus") + 1] == "0.3"
    assert b"rtsp://admin:ABCDEF@192.0.2.10:8564/livingroom'" in payload   # the relay, never the camera
    assert b"audio" not in payload


# ── TC-1745 the unit ─────────────────────────────────────────────────────────


def test_tc1745_the_unit_is_knob_gated_runs_the_venv_and_refusals_do_not_loop():
    u = (STACK / "camera-presence" / "homehub-camera-presence@.service").read_text(encoding="utf-8")
    live = [l for l in u.splitlines() if not l.startswith("#")]
    text = "\n".join(live)
    assert "camera_presence.py --camera %i condition" in text
    assert "ExecStart=/opt/homehub-presence/venv/bin/python /opt/homehub/stack/camera-presence/camera_presence.py" \
           " --camera %i run" in text
    assert "RestartPreventExitStatus=78" in live and "StateDirectory=homehub-presence" in live
    assert "Wants=docker.service network-online.target homehub-camera-relay@%i.service" in live
    assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]


# ── TC-1746 the template ─────────────────────────────────────────────────────


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1746_the_template_ships_presence_off_with_the_refused_placeholder():
    assert env_example("CAMERA_LIVINGROOM_PRESENCE_ENABLED") == "false"
    assert env_example("CAMERA_LIVINGROOM_PRESENCE_FEED_PORT") == "0"
    assert env_example("CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON") == '"0,0;1,0;1,1;0,1"'
    assert env_example("CAMERA_LIVINGROOM_PRESENCE_EXCLUDE_POLYGONS") == ""
    template = {k: env_example(k) for k in ("CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON",)}
    s = cp.PresenceSettings({k: v.strip('"') for k, v in template.items()}, "livingroom")
    assert s.placeholder


# ── TC-1747 the run refuses before it decodes anything ───────────────────────


def test_tc1747_run_refuses_the_placeholder_with_78_and_starts_nothing(tmp_path, monkeypatch):
    path = tmp_path / "env"
    path.write_text("LAN_IP=192.0.2.10\nCAMERA_LIVINGROOM_RELAY_RTSP_PORT=8564\nCAMERA_LIVINGROOM_RELAY_API_PORT=1994\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST=192.0.2.69\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH=/h264/ch1/sub/av_stream\n"
                    "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD=ABCDEF\nCAMERA_LIVINGROOM_RELAY_EXPECT_SIZE=768x432\n"
                    'CAMERA_LIVINGROOM_PRESENCE_ROOM_POLYGON="0,0;1,0;1,1;0,1"\n')
    started = []
    monkeypatch.setattr(dr, "start_consumer", lambda *a, **k: started.append(a))
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: started.append(a))
    assert cp.run_service(str(path), "livingroom", state_dir=str(tmp_path / "state")) == cp.EXIT_REFUSED
    assert started == []
