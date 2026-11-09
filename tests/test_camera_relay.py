"""A camera's own go2rtc relay beside the door relay (SN-055, SR-155, SR-156).

What runs here, on the dev PC: the per-camera settings and their refusals (keys,
never values), the rendered config (every listener named and authenticated, the
door relay's hardening, audio offered only when the knob says so and withheld
from a consumer that does not ask), the consumers' argv (no credential), the
audio verdict, the redaction, and the unit/compose/template contract.

What does not: the real relay against the real camera. That is the deploy
step's `camera_relay.py probe` and `audio-check` on the hub (HomeHub
LIVING_ROOM_PRESENCE_PLAN_2026-11-09 section 6).
Implements: TC-1730..TC-1735.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-relay", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_relay as cr  # noqa: E402
import door_relay as dr  # noqa: E402

PASSWORD = "ABCDEF"  # an upper-case six-letter code, as the camera's is


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "DOOR_RELAY_RTSP_PORT": "8554",
        "DOOR_RELAY_API_PORT": "1984",
        "CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8564",
        "CAMERA_LIVINGROOM_RELAY_API_PORT": "1994",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.69",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/h264/ch1/sub/av_stream",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": PASSWORD,
        "CAMERA_LIVINGROOM_RELAY_AUDIO": "true",
        "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": "768x432",
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items() if kv[1] is not None))
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1730 settings and refusals ────────────────────────────────────────────


def test_tc1730_a_valid_camera_reads_its_own_keys(tmp_path):
    s = cr.CameraRelaySettings(write_env(tmp_path), "livingroom")
    assert (s.rtsp_port, s.api_port, s.stream, s.port, s.username) == (8564, 1994, "livingroom", 554, "admin")
    assert s.password == PASSWORD and s.audio is True
    assert (s.expect_width, s.expect_height) == (768, 432)
    assert s.upstream_url() == "rtsp://admin:%s@192.0.2.69:554/h264/ch1/sub/av_stream" % PASSWORD


@pytest.mark.parametrize("over, key", [
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": None}, "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD"),
    ({"CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8554"}, "CAMERA_LIVINGROOM_RELAY_RTSP_PORT"),
    ({"CAMERA_LIVINGROOM_RELAY_API_PORT": "1984"}, "CAMERA_LIVINGROOM_RELAY_API_PORT"),
    ({"CAMERA_LIVINGROOM_RELAY_API_PORT": "8564"}, "CAMERA_LIVINGROOM_RELAY_RTSP_PORT"),
    ({"CAMERA_LIVINGROOM_RELAY_RTSP_PORT": None}, "CAMERA_LIVINGROOM_RELAY_RTSP_PORT"),
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "camera.lan"}, "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST"),
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.10"}, "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST"),
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": None}, "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH"),
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/a?b"}, "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH"),
    ({"CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": None}, "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE"),
])
def test_tc1730_refusals_name_the_key_and_never_the_value(tmp_path, over, key):
    with pytest.raises(cr.Invalid) as err:
        cr.CameraRelaySettings(write_env(tmp_path, **over), "livingroom")
    assert key in str(err.value)
    assert PASSWORD not in str(err.value) and "192.0.2.69" not in str(err.value)


@pytest.mark.parametrize("name", ["door", "living-room", "Livingroom", "", "a" * 17])
def test_tc1730_camera_names_are_knob_safe_and_never_the_door(name):
    with pytest.raises(cr.Invalid):
        cr.camera_name(name)


# ── TC-1731 the rendered config ──────────────────────────────────────────────


def test_tc1731_every_listener_is_named_and_authenticated(tmp_path):
    cfg = cr.relay_config(cr.CameraRelaySettings(write_env(tmp_path), "livingroom"))
    assert cfg["api"] == {"listen": "127.0.0.1:1994", "username": "admin", "password": PASSWORD, "local_auth": True}
    assert cfg["rtsp"]["listen"] == "192.0.2.10:8564"
    assert (cfg["rtsp"]["username"], cfg["rtsp"]["password"]) == ("admin", PASSWORD)
    for module in ("webrtc", "srtp", "rtmp"):
        assert cfg[module] == {"listen": ""}
    # A consumer that does not ask gets video only, audio passed or not.
    assert cfg["rtsp"]["default_query"] == "video"
    assert set(cfg["streams"]) == {"livingroom"}


def test_tc1731_audio_is_offered_only_when_the_knob_says_so(tmp_path):
    on = cr.relay_config(cr.CameraRelaySettings(write_env(tmp_path), "livingroom"))["streams"]["livingroom"]
    off = cr.relay_config(cr.CameraRelaySettings(write_env(tmp_path, CAMERA_LIVINGROOM_RELAY_AUDIO="false"),
                                                 "livingroom"))["streams"]["livingroom"]
    assert "#" not in on and on.endswith("/h264/ch1/sub/av_stream")
    assert off.endswith("#media=video")
    # Anything but exactly `true` is video-only.
    odd = cr.CameraRelaySettings(write_env(tmp_path, CAMERA_LIVINGROOM_RELAY_AUDIO="yes"), "livingroom")
    assert odd.audio is False


def test_tc1731_the_door_relay_config_is_not_changed_by_reuse(tmp_path):
    door_env = tmp_path / "door.env"
    door_env.write_text("LAN_IP=192.0.2.10\nDOOR_RELAY_UPSTREAM_HOST=192.0.2.20\nDOOR_RELAY_UPSTREAM_PASSWORD=x\n")
    cfg = dr.relay_config(dr.RelaySettings(str(door_env)))
    assert cfg["streams"]["door"].endswith("#media=video") and cfg["rtsp"]["listen"] == "192.0.2.10:8554"


# ── TC-1732 consumers carry no credential in argv ────────────────────────────


def assert_no_secret(argv, s):
    joined = "\n".join(argv)
    assert "rtsp://" not in joined and not any(f in joined for f in dr.secret_forms(s))
    entry = argv.index("--entrypoint")
    assert argv[entry + 1] in ("ffmpeg", "ffprobe") and "-i" in argv[:entry]
    assert not {"-e", "--env", "--env-file"} & set(argv[:entry])


def test_tc1732_the_audio_probe_asks_only_in_the_stdin_document(tmp_path, monkeypatch):
    s = cr.CameraRelaySettings(write_env(tmp_path), "livingroom")
    seen = []

    def fake_run(argv, input=None, **_kw):
        seen.append((argv, input))

        class Done:
            returncode = 0
            stdout = b'{"streams":[{"codec_type":"video","codec_name":"h264"},' \
                     b'{"codec_type":"audio","codec_name":"aac","sample_rate":"16000","channels":1}]}'
            stderr = b""
        return Done()
    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    asked = cr.audio_probe(s, 3, True, "homehub-camera-probe-livingroom")
    plain = cr.audio_probe(s, 3, False, "homehub-camera-probe-livingroom")
    for argv, doc in seen:
        assert_no_secret(argv, s)
    assert b"/livingroom?video&audio'" in seen[0][1] and b"?video" not in seen[1][1]
    assert s.consumer_query == "", "the probe must not change the caller's settings"
    assert [t["codec_type"] for t in asked["tracks"]] == ["video", "audio"] and plain["rc"] == 0


# ── TC-1733 the audio verdict ────────────────────────────────────────────────


def probe(*kinds):
    return {"rc": 0, "tracks": [{"codec_type": k} for k in kinds]}


@pytest.mark.parametrize("withheld, asked, passes, ok", [
    (probe("video"), probe("video", "audio"), True, True),
    (probe("video", "audio"), probe("video", "audio"), True, False),   # default_query not honoured
    (probe("video"), probe("video"), True, False),                     # audio lost in the relay
    (probe("video"), probe("video"), False, True),                     # video-only relay
    (probe("video"), probe("video", "audio"), False, False),           # #media=video not honoured
    (probe(), probe("video", "audio"), True, False),                   # no video at all
    ({"rc": 1}, probe("video", "audio"), True, False),                 # a probe that did not finish
])
def test_tc1733_audio_verdict(withheld, asked, passes, ok):
    assert (cr.audio_verdict(withheld, asked, passes) == []) is ok


# ── TC-1734 redaction covers this camera ─────────────────────────────────────


def test_tc1734_relay_output_loses_every_spelling_of_the_code_and_the_address(tmp_path):
    s = cr.CameraRelaySettings(write_env(tmp_path), "livingroom")
    line = ("WRN [rtsp] url=rtsp://admin:%s@192.0.2.69:554/h264/ch1/sub/av_stream error=EOF host 192.0.2.69 "
            "token %s" % (PASSWORD, dr.secret_forms(s)[0]))
    clean = dr.redact(line, s)
    assert PASSWORD not in clean and "192.0.2.69" not in clean
    assert "<camera>:554" in clean


# ── TC-1735 unit, compose and template ───────────────────────────────────────


def unit():
    return (STACK / "camera-relay" / "homehub-camera-relay@.service").read_text(encoding="utf-8")


def compose_block(name):
    text = (STACK / "docker-compose.yml").read_text(encoding="utf-8")
    start = text.index("\n  %s:\n" % name)
    end = re.search(r"\n  [a-z0-9-]+:\n|\n[a-z]+:\n", text[start + 3:]).start() + start + 3
    return text[start:end]


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1735_the_unit_is_knob_gated_redacted_and_never_requires_docker():
    live = [l for l in unit().splitlines() if not l.startswith("#")]
    text = "\n".join(live)
    assert "camera_relay.py --camera %i condition" in text
    assert "redact-run -- /usr/bin/docker compose --profile camera-relay-%i up" in text
    assert "RuntimeDirectory=homehub-camera-relay-%i" in live
    assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]
    assert cr.run_dir("livingroom") == "/run/homehub-camera-relay-livingroom"


def test_tc1735_the_compose_service_has_the_door_relays_hardening():
    block = compose_block("camera-relay-livingroom")
    assert "image: ${DOOR_RELAY_IMAGE}" in block
    assert 'profiles: ["camera-relay-livingroom"]' in block and 'restart: "no"' in block
    assert "network_mode: host" in block and "ports:" not in block
    assert "container_name: %s" % cr.container("livingroom") in block
    assert "source: %s/go2rtc.yaml" % cr.run_dir("livingroom") in block
    assert "create_host_path: false" in block and "read_only: true" in block
    assert "cap_drop:\n      - ALL" in block and "driver: none" in block
    assert "camera-relay-livingroom" not in (env_example("COMPOSE_PROFILES") or "").split(",")


def test_tc1735_the_template_ships_it_off_audio_on_and_off_the_door_ports():
    assert env_example("CAMERA_LIVINGROOM_RELAY_ENABLED") == "false"
    assert env_example("CAMERA_LIVINGROOM_RELAY_AUDIO") == "true"
    assert env_example("CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST") == ""
    assert env_example("CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD") == ""
    assert {env_example("CAMERA_LIVINGROOM_RELAY_RTSP_PORT"), env_example("CAMERA_LIVINGROOM_RELAY_API_PORT")} \
        .isdisjoint({env_example("DOOR_RELAY_RTSP_PORT"), env_example("DOOR_RELAY_API_PORT")})
    # Never enrolled in motion clips (a recording) by the template.
    assert "livingroom" not in (env_example("CAMERAS") or "").split()


# ── TC-1735 the code reaches the live .env by stdin only ─────────────────────


def test_tc1735_import_password_writes_the_key_from_stdin_and_prints_no_value(tmp_path, capsys, monkeypatch):
    path = write_env(tmp_path, CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD="", OTHER="kept")
    monkeypatch.setattr(cr.sys, "stdin", type("S", (), {"buffer": __import__("io").BytesIO(b"AB$CD EF\n")})())
    assert cr.main(["--env-file", path, "--camera", "livingroom", "import-password"]) == 0
    text = open(path, encoding="utf-8").read()
    assert 'CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD="AB$$CD EF"' in text and "OTHER=kept" in text
    assert text.count("CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD=") == 1
    assert cr.CameraRelaySettings(path, "livingroom").password == "AB$CD EF"
    out = capsys.readouterr()
    assert "AB$CD" not in out.out + out.err and "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD" in out.out


@pytest.mark.parametrize("blob", [b"", b"\n", b"A\nB", b"x" * 2000])
def test_tc1735_import_password_refuses_empty_multiline_and_oversize(tmp_path, blob):
    path = write_env(tmp_path)
    before = open(path, encoding="utf-8").read()
    with pytest.raises(cr.Invalid):
        cr.import_password(path, "livingroom", blob)
    assert open(path, encoding="utf-8").read() == before
