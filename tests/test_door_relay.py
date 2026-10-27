"""The door relay's host side and the recorder's pure core (SN-050, SR-105..SR-107).

What runs here, on the dev PC: the relay config (every listener named, auth on
both, video only, the credential encoded), the field validation that names keys
and never values, the panel-to-hub credential import, the pin agreeing with the
compose image, the unit/compose/firstboot contract, and the recorder's MPEG-TS
cutter, pruning, guard-before-every-segment, quota stop and continuity rule -
on synthetic TS packets built below.

What does not: the real relay, ffmpeg and a camera-like source. That is
stack/door/tests/door-relay-lab.sh (root + docker; TC-1385..TC-1392).
Implements: TC-1377..TC-1384, TC-1393..TC-1399.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("door", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import door_relay as dr  # noqa: E402
import door_recorder as rec  # noqa: E402

PASSWORD = 'c@m p$ss:1 #x'


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "DOOR_RELAY_UPSTREAM_HOST": "192.0.2.20",
        "DOOR_RELAY_UPSTREAM_USERNAME": "admin",
        # As Materialize-Deploy writes it: $ doubled, whitespace -> one quote pair.
        "DOOR_RELAY_UPSTREAM_PASSWORD": '"c@m p$$ss:1 #x"',
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items()))
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1380 the relay config ─────────────────────────────────────────────────


def test_tc1380_every_listener_is_named_bound_and_authenticated(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    cfg = dr.relay_config(s)
    assert cfg["rtsp"]["listen"] == "192.0.2.10:8554"
    assert cfg["api"]["listen"] == "127.0.0.1:1984"
    assert cfg["api"]["local_auth"] is True  # loopback is NOT exempt from the API credential
    for mod in ("rtsp", "api"):
        assert cfg[mod]["username"] == "admin" and cfg[mod]["password"] == PASSWORD
    for mod in ("webrtc", "srtp", "rtmp"):
        assert cfg[mod] == {"listen": ""}
    assert set(cfg) == {"api", "rtsp", "webrtc", "srtp", "rtmp", "log", "streams"}


def test_tc1380_video_only_and_the_credential_percent_encoded(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    src = dr.relay_config(s)["streams"]["door"]
    assert src.endswith("#media=video")
    assert src.startswith("rtsp://admin:c%40m%20p%24ss%3A1%20%23x@192.0.2.20:554/H.264")
    assert dr.relay_config(s)["rtsp"]["default_query"] == "video"
    assert s.consumer_url() == "rtsp://admin:c%40m%20p%24ss%3A1%20%23x@192.0.2.10:8554/door"


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1380_render_writes_0600(tmp_path):
    out = tmp_path / "run" / "go2rtc.yaml"
    assert dr.main(["--env-file", write_env(tmp_path), "render", "--out", str(out)]) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert json.loads(out.read_text())["rtsp"]["password"] == PASSWORD


# ── TC-1381 validation names the key, never the value ────────────────────────


@pytest.mark.parametrize("over, key", [
    ({"LAN_IP": "hub.lan"}, "LAN_IP"),
    ({"DOOR_RELAY_UPSTREAM_HOST": ""}, "DOOR_RELAY_UPSTREAM_HOST"),
    ({"DOOR_RELAY_UPSTREAM_HOST": "192.0.2.10"}, "own address"),
    ({"DOOR_RELAY_UPSTREAM_PORT": "70000"}, "DOOR_RELAY_UPSTREAM_PORT"),
    ({"DOOR_RELAY_UPSTREAM_PATH": "H.264"}, "DOOR_RELAY_UPSTREAM_PATH"),
    ({"DOOR_RELAY_UPSTREAM_PASSWORD": ""}, "DOOR_RELAY_UPSTREAM_PASSWORD"),
    ({"DOOR_RELAY_STREAM": "Door Cam"}, "DOOR_RELAY_STREAM"),
])
def test_tc1381_a_bad_field_is_refused_by_name(tmp_path, over, key):
    with pytest.raises(dr.Invalid) as err:
        dr.RelaySettings(write_env(tmp_path, **over))
    assert key in str(err.value)
    assert "p$ss" not in str(err.value) and "c@m" not in str(err.value)


def test_tc1381_the_secret_reader_undoes_exactly_the_materialisers_encoding():
    assert dr.env_secret_value('"a b$$c"') == "a b$c"
    assert dr.env_secret_value("a$$b#c") == "a$b#c"   # a ' #' is NOT a comment in a secret
    assert dr.env_secret_value(dr.env_encode("x y $z")) == "x y $z"


# ── TC-1382 import-upstream through a pipe ───────────────────────────────────


def records(**over):
    base = {"host": "192.0.2.20", "port": "554", "path": "/H.264", "username": "admin", "password": PASSWORD}
    base.update(over)
    return b"".join(("%s=%s" % kv).encode() + b"\0" for kv in base.items())


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1382_import_writes_the_five_keys_encoded_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LAN_IP=192.0.2.10\nDOOR_RELAY_UPSTREAM_HOST=\nOTHER=1\n")
    keys = dr.import_upstream(str(env), records())
    assert keys == sorted(dr.UPSTREAM_KNOB.values())
    text = env.read_text()
    assert "OTHER=1\n" in text and text.count("DOOR_RELAY_UPSTREAM_HOST=") == 1
    assert 'DOOR_RELAY_UPSTREAM_PASSWORD="c@m p$$ss:1 #x"\n' in text
    s = dr.RelaySettings(str(env))
    assert s.password == PASSWORD and s.host == "192.0.2.20"


@pytest.mark.parametrize("blob, words", [
    (records().replace(b"username=admin\0", b""), "missing"),
    (records() + b"host=192.0.2.21\0", "duplicate"),
    (records() + b"shell=1\0", "unexpected"),
    (records(password="a\nb"), "control"),
    (records(host="cam.lan"), "IPv4"),
])
def test_tc1382_bad_records_are_refused_and_the_file_is_untouched(tmp_path, blob, words):
    env = tmp_path / ".env"
    env.write_text("LAN_IP=192.0.2.10\n")
    with pytest.raises((dr.Invalid, UnicodeDecodeError)) as err:
        dr.import_upstream(str(env), blob)
    assert words in str(err.value)
    assert env.read_text() == "LAN_IP=192.0.2.10\n"
    assert PASSWORD not in str(err.value)


# ── TC-1383 the pin, the compose image and the knob agree ────────────────────


def pin():
    out = {}
    for line in (STACK / "door" / "door-relay-image.pin").read_text().splitlines():
        m = re.match(r"([A-Z0-9_]+)=(\S+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1383_pin_has_version_and_index_digest_and_the_knob_is_built_from_it():
    p = pin()
    assert p["IMAGE"] == "alexxit/go2rtc" and re.fullmatch(r"\d+\.\d+\.\d+", p["VERSION"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", p["DIGEST"])
    assert env_example("DOOR_RELAY_IMAGE") == "%s:%s@%s" % (p["IMAGE"], p["VERSION"], p["DIGEST"])
    assert dr.image_ref() == env_example("DOOR_RELAY_IMAGE")


# ── TC-1377 / TC-1378 units, compose and firstboot ───────────────────────────


def compose_block(name):
    text = (STACK / "docker-compose.yml").read_text(encoding="utf-8")
    start = text.index("\n  %s:\n" % name)
    end = re.search(r"\n  [a-z0-9-]+:\n|\n[a-z]+:\n", text[start + 3:]).start() + start + 3
    return text[start:end]


def test_tc1378_the_relay_service_is_systemd_owned_pinned_and_hostnet():
    block = compose_block("door-relay")
    assert 'image: ${DOOR_RELAY_IMAGE}' in block
    assert 'profiles: ["door-relay"]' in block and 'restart: "no"' in block
    assert "network_mode: host" in block and "ports:" not in block
    assert "create_host_path: false" in block and "read_only: true" in block
    assert "cap_drop:\n      - ALL" in block
    profiles = env_example("COMPOSE_PROFILES") or ""
    assert "door-relay" not in profiles.split(",")


def unit(name):
    return (STACK / "door" / name).read_text(encoding="utf-8")


def test_tc1377_the_recorder_cannot_start_ahead_of_the_mount_or_outlive_it(tmp_path):
    u = unit("homehub-door-recorder.service")
    for line in ("RequiresMountsFor=/srv/scratch", "BindsTo=srv-scratch.mount",
                 "CapabilityBoundingSet=~CAP_SYS_RESOURCE", "RestartPreventExitStatus=75 76",
                 "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/scratch/homehub_scratch.py guard --writer door",
                 "WantedBy=multi-user.target srv-scratch.mount"):
        assert line in u.splitlines(), line
    assert "After=srv-scratch.mount" in u
    # The recorder's container has no restart policy of its own and mounts nothing.
    argv, _payload = rec.ffmpeg_command(dr.RelaySettings(write_env(tmp_path)))
    assert "--restart" not in argv and "-v" not in argv and "--volume" not in argv
    assert "--rm" in argv and "--read-only" in argv and "ALL" in argv
    # No secret in it: the full check, for every consumer, is TC-1384's.
    assert_consumer_argv_carries_no_secret(argv, dr.RelaySettings(write_env(tmp_path)))


def test_tc1377_neither_unit_requires_docker_and_both_have_their_knob_condition():
    for name, knob in (("homehub-door-relay.service", "DOOR_RELAY_ENABLED"),
                       ("homehub-door-recorder.service", "DOOR_RECORDER_ENABLED")):
        live = [l for l in unit(name).splitlines() if not l.startswith("#")]
        assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]
        u = "\n".join(live)
        assert "condition %s" % knob in u


def test_tc1379_firstboot_makes_scratch_after_the_docker_lv_and_installs_the_units():
    fb = (STACK / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    assert fb.index("\nsetup_docker_lv\n") < fb.index("homehub_scratch.py\" --env-file") < fb.index("# ── 1c.")
    assert 'if [ "$__scratch_on" = true ]' in fb
    assert "systemctl start --no-block homehub-door-relay.service homehub-door-recorder.service" in fb
    for name in ("homehub-door-relay.service", "homehub-door-recorder.service"):
        assert "systemctl enable %s" % name in fb


def test_tc1397_the_image_bake_follows_the_relay_knob():
    text = (ROOT / "vmtest" / "export-images.sh").read_text(encoding="utf-8")
    assert "DOOR_RELAY_ENABLED" in text and "--profile door-relay" in text


# ── TC-1384 measurement helpers carry no secret ──────────────────────────────


def test_tc1384_redact_with_the_settings_removes_every_spelling_and_the_camera(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    # go2rtc 1.9.14's producer warning on a camera dropout, as the lab showed it
    line = ('WRN [streams] error="dial tcp 192.0.2.20:554: connect: connection refused" '
            'url=%s#media=video' % s.upstream_url())
    out = dr.redact(line, s)
    for leak in (PASSWORD, "c%40m%20p%24ss%3A1%20%23x", "192.0.2.20"):
        assert leak not in out, leak
    assert "<camera>:554" in out and "rtsp://***@" in out
    token = __import__("base64").b64encode(("admin:" + PASSWORD).encode()).decode()
    for raw in (PASSWORD, "c%40m%20p%24ss%3A1%20%23x", token):
        assert dr.redact("a %s b" % raw, s) == "a *** b", raw
    assert dr.redact("peer 192.0.2.20 closed", s) == "peer <camera> closed"
    # a longer address that merely starts with the camera's is left alone
    assert dr.redact("192.0.2.201:554", s) == "192.0.2.201:554"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX child process and exit status")
def test_tc1384_redact_run_passes_nothing_secret_and_keeps_the_exit_status(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    child = ("import sys; print('WRN url=%s'); sys.stdout.flush(); "
             "sys.stderr.write('ERR pw=%s at 192.0.2.20:554\\n'); sys.exit(3)" % (s.upstream_url(), PASSWORD))
    out = io.StringIO()
    assert dr.redact_run(s, [sys.executable, "-c", child], out) == 3
    text = out.getvalue()
    assert PASSWORD not in text and "%24ss" not in text and "192.0.2.20" not in text
    assert "WRN url=rtsp://***@<camera>:554/H.264" in text and "ERR pw=*** at <camera>:554" in text  # both streams


def test_tc1384_the_relay_output_has_no_path_but_redact_run():
    # The unit's ExecStart is the redacting wrapper around the attached `up`...
    live = [l for l in unit("homehub-door-relay.service").splitlines() if l.startswith("ExecStart=")]
    assert live == ["ExecStart=/usr/bin/python3 /opt/homehub/stack/door/door_relay.py --env-file "
                    "/opt/homehub/stack/.env redact-run -- /usr/bin/docker compose --profile door-relay up "
                    "--no-deps --no-log-prefix --exit-code-from door-relay door-relay"]
    # ...the container keeps no copy of its own (no json-file / local driver)...
    assert "logging:\n      driver: none" in compose_block("door-relay")


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1384_the_render_line_names_no_camera_address(tmp_path, capsys):
    dr.main(["--env-file", write_env(tmp_path), "render", "--out", str(tmp_path / "run" / "go2rtc.yaml")])
    assert "192.0.2.20" not in capsys.readouterr().out


def test_tc1384_redact_and_the_upstream_count():
    assert dr.redact("x rtsp://admin:p%40ss@192.0.2.20:554/H.264 y") == "x rtsp://***@192.0.2.20:554/H.264 y"
    ss = ("0 0 192.0.2.10:40000 192.0.2.20:554\n"
          "0 0 192.0.2.10:40002 192.0.2.30:554\n"
          "0 0 192.0.2.10:8554 192.0.2.17:51000\n")
    assert dr.count_established(ss, "192.0.2.20", 554) == 1
    assert dr.count_established(ss, "192.0.2.20", 8554) == 0


# ── TC-1384 every consumer: the URL on stdin, never in argv or env ───────────
#
# A value in argv is in /proc/<pid>/cmdline for any local user, and a
# container's processes are host processes; an env var is in `docker inspect`.
# So the checks are on the WHOLE argv - the docker client's, which is also the
# container's command - and on what a shell inside it could expand. The real
# running process is checked by door-relay-lab.sh check 11.


def assert_consumer_argv_carries_no_secret(argv, s):
    joined = "\n".join(argv)
    assert "rtsp://" not in joined
    for form in dr.secret_forms(s) + [s.consumer_url()]:
        assert form not in joined, "a spelling of the credential is in argv"
    # Nothing a shell could expand into an argument: no shell, and no `$`.
    entry = argv.index("--entrypoint")
    assert argv[entry + 1] in ("ffmpeg", "ffprobe")
    assert not any(a in ("sh", "bash", "/bin/sh", "/bin/bash") or a.endswith("/sh") for a in argv)
    assert not any("$" in a for a in argv)
    # No environment or env file handed to the container at all.
    assert not {"-e", "--env", "--env-file"} & set(argv[:entry])
    # The docker client attaches stdin (-i), and the tool reads its input there,
    # as an ffconcat document.
    assert "-i" in argv[:entry]
    tool = argv[entry + 2:]
    at = tool.index("-i")
    assert tool[at + 1] == "pipe:0" and tool[at - 6:at] == list(dr.FFCONCAT_INPUT[:-2])


def assert_payload_carries_the_url(payload, s):
    text = payload.decode("utf-8")
    assert text.startswith("ffconcat version 1.0\n")
    assert "\nfile '%s'\n" % s.consumer_url() in text
    assert "\noption rtsp_transport tcp\n" in text


class FakeStdin:
    def __init__(self):
        self.data, self.closed = b"", False

    def write(self, data):
        assert not self.closed
        self.data += data
        return len(data)

    def close(self):
        self.closed = True


class RecordingPopen:
    """A started consumer: what it was given on stdin, and whether it was closed."""
    started = []

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, **_kw):
        self.argv = list(argv)
        self.stdin = FakeStdin() if stdin == dr.subprocess.PIPE else None
        self.stdout = rec_empty_stream() if stdout == dr.subprocess.PIPE else None
        self.stderr = io.BytesIO(b"") if stderr == dr.subprocess.PIPE else None
        RecordingPopen.started.append(self)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def rec_empty_stream():
    import tempfile
    return tempfile.TemporaryFile()  # select() ready, os.read() -> b"": the stream ended


def assert_consumer_started_by_stdin(proc, s):
    assert_consumer_argv_carries_no_secret(proc.argv, s)
    assert proc.stdin is not None and proc.stdin.closed, "the document must be written and stdin closed"
    assert_payload_carries_the_url(proc.stdin.data, s)


def test_tc1384_the_input_document_and_its_one_builder(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    argv, payload = dr.relay_consumer(s, "x", "ffmpeg", ["-nostdin"], ["-f", "null", "-"], timeout_us=15000000)
    assert_consumer_argv_carries_no_secret(argv, s)
    assert_payload_carries_the_url(payload, s)
    assert payload.decode().endswith("option timeout 15000000\n")
    with pytest.raises(ValueError):
        dr.relay_consumer(s, "x", "/bin/sh", ["-c"], ["exec ffmpeg"])


def test_tc1384_the_recorders_reader_carries_the_url_only_in_its_payload(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    argv, payload = rec.ffmpeg_command(s)
    assert_consumer_argv_carries_no_secret(argv, s)
    assert_payload_carries_the_url(payload, s)
    assert argv[-4:] == ["-an", "-f", "mpegts", "pipe:1"]  # stdout is the TS; stdin only the document
    assert "option timeout 15000000" in payload.decode()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="select() on the reader's stdout is POSIX")
def test_tc1384_the_recorder_service_hands_the_document_to_ffmpegs_stdin(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    RecordingPopen.started = []
    monkeypatch.setattr(dr.subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(rec.Recorder, "guard", lambda self: (True, "ok"))
    monkeypatch.setattr(rec.signal, "signal", lambda *a: None)
    assert rec.run_service(write_env(tmp_path)) == 1  # the fake stream ends at once
    (proc,) = RecordingPopen.started
    assert proc.argv[proc.argv.index("--name") + 1] == rec.CONTAINER
    assert_consumer_started_by_stdin(proc, s)


def test_tc1384_the_probe_hands_the_document_to_stdin(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    seen = {}

    def run(argv, input=None, **_kw):
        seen.update(argv=list(argv), input=input)
        out = json.dumps({"streams": [{"codec_name": "h264", "width": 1536, "height": 1536,
                                       "nb_read_packets": "51", "avg_frame_rate": "10/1"}]})
        return dr.subprocess.CompletedProcess(argv, 0, out.encode(), b"")
    monkeypatch.setattr(dr.subprocess, "run", run)
    result = dr.probe(s, 7)
    assert dr.probe_verdict(result, s) == [] and result["packets"] == 51
    assert_consumer_argv_carries_no_secret(seen["argv"], s)
    assert_payload_carries_the_url(seen["input"], s)
    assert seen["argv"][seen["argv"].index("-read_intervals") + 1] == "%+7"


def test_tc1384_the_cost_decoder_hands_the_document_to_stdin(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    RecordingPopen.started = []
    monkeypatch.setattr(dr.subprocess, "Popen", RecordingPopen)
    proc = dr.start_grey_decoder(s, 5)
    assert proc is RecordingPopen.started[0]
    assert_consumer_started_by_stdin(proc, s)
    assert "fps=5,scale=256:256,format=gray" in proc.argv


# ── the recorder: synthetic MPEG-TS ──────────────────────────────────────────

sys.path.insert(0, str(STACK / "door" / "tests"))
from ts_fixture import PMT_PID, VIDEO_PID, body, pat, pes, pmt, stream  # noqa: E402


def test_tc1393_packet_fields_pat_pmt_and_pts():
    assert rec.parse_pat(pat()) == PMT_PID
    assert rec.parse_pmt(pmt()) == VIDEO_PID
    assert rec.parse_pmt(pmt(stream_type=0x0F)) is None  # AAC: not video
    pid, pusi, rai = rec.packet_fields(pes(0, True))
    assert (pid, pusi, rai) == (VIDEO_PID, True, True)
    assert rec.packet_fields(pes(0, False))[2] is False
    assert rec.pes_pts(pes(123456, True)) == 123456


def test_tc1393_the_packetiser_realigns_after_junk_and_torn_reads():
    data = b"\x00junk" + b"".join(stream(3))
    pk = rec.Packetiser()
    got = []
    for i in range(0, len(data), 77):  # arbitrary pipe read sizes
        got += list(pk.feed(data[i:i + 77]))
    assert got == stream(3) and pk.dropped == 5


def test_tc1393_segments_start_on_a_keyframe_with_pat_pmt_and_cut_on_the_clock():
    # 7 s, so the boundary (packet 70) falls mid-GOP, two packets after a
    # NON-key PES: a cut on any PES would land there, a correct one waits.
    split = rec.Splitter(segment_sec=7)
    actions = []
    t = 994.0  # a multiple of 7: boundaries are on the clock, so the next is t + 7
    pkts = [body(), body()] + stream(40)  # junk before the first PAT is dropped
    for n, pkt in enumerate(pkts):
        now = t + n * 0.1
        action, header = split.feed(pkt, now)
        actions.append(action)
        if action == "start":
            assert header == pat() + pmt()
            assert rec.packet_fields(pkt)[2]  # a keyframe
    starts = [i for i, a in enumerate(actions) if a == "start"]
    assert actions[0] == actions[1] == "drop"
    assert len(starts) >= 2
    # the second cut is the first keyframe at/after the t + 7 boundary: GOP 3
    assert t + starts[1] * 0.1 >= t + 7
    assert starts[1] == 2 + 3 * 32 + 2  # 2 junk, three 32-packet GOPs, then PAT+PMT


def test_tc1394_pruning_removes_whole_oldest_segments_only(tmp_path):
    for day, names in (("2026-10-21", ["door-20261021T230000Z.ts", "door-20261021T231000Z.ts"]),
                       ("2026-10-22", ["door-20261022T000000Z.ts"])):
        (tmp_path / day).mkdir()
        for n in names:
            (tmp_path / day / n).write_bytes(b"x" * 100)
    (tmp_path / "2026-10-22" / "door-20261022T001000Z.ts.part").write_bytes(b"x" * 100)
    removed = rec.prune(str(tmp_path), cap_bytes=200, headroom=50, current=0)
    assert [os.path.basename(p) for p in removed] == ["door-20261021T230000Z.ts", "door-20261021T231000Z.ts"]
    assert not (tmp_path / "2026-10-21").exists()
    assert (tmp_path / "2026-10-22" / "door-20261022T001000Z.ts.part").exists()
    assert rec.prune(str(tmp_path), cap_bytes=10 ** 6, headroom=0) == []


class FakeStream:
    """A readable fd over bytes for Recorder.consume. A regular file, not a pipe:
    select() reports it ready, os.read() ends with b"" (the stream ended), and
    nothing blocks on a pipe buffer the way a 100 KB os.write() into a pipe would."""

    def __init__(self, data):
        import tempfile
        self.file = tempfile.TemporaryFile()
        self.file.write(data)
        self.file.seek(0)

    def fileno(self):
        return self.file.fileno()


needs_select_on_pipes = pytest.mark.skipif(sys.platform.startswith("win"), reason="select() on pipes is POSIX")


def make_recorder(tmp_path, guard_results, alerts, **knobs):
    settings = rec.RecorderSettings(dict({"DOOR_RECORDER_SEGMENT_SEC": "10"}, **knobs))
    r = rec.Recorder("/nonexistent", str(tmp_path / "door"), settings, None,
                     alert=lambda *a: alerts.append(a), clock=iter([]).__next__)
    results = iter(guard_results)
    r.guard = lambda: next(results)
    return r


@needs_select_on_pipes
def test_tc1395_the_guard_runs_before_every_segment_and_a_refusal_stops_before_opening(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok"), (False, "/srv/scratch is not a mount point")], alerts)
    clock = iter([1000.0 + i * 20.0 for i in range(10000)])  # 20 s per 64 KiB read
    r.clock = lambda: next(clock)
    data = b"".join(stream(200))  # 20 s of frames -> a second segment is due
    with pytest.raises(SystemExit) as code:
        r.consume(FakeStream(data))
    assert code.value.code == rec.EXIT_GUARD
    files = sorted(p.name for p in (tmp_path / "door").rglob("door-*"))
    assert len(files) == 1 and files[0].endswith(".ts")  # the first closed; no second opened
    assert alerts and alerts[0][0] == "door-recorder-guard"


class QuotaFull:
    """A segment file whose next write hits EDQUOT; everything else is real."""

    def __init__(self, real):
        self.real = real

    def write(self, data):
        raise OSError(errno.EDQUOT, "Disk quota exceeded")

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.skipif(not hasattr(errno, "EDQUOT"), reason="EDQUOT is POSIX")
def test_tc1395_a_full_quota_stops_cleanly_writes_nothing_else_and_alerts(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok")], alerts)
    r.open_segment(1000.0)
    r.handle = QuotaFull(r.handle)
    with pytest.raises(SystemExit) as code:
        r.write(b"G" * 188)
    assert code.value.code == rec.EXIT_QUOTA
    assert [a[0] for a in alerts] == ["door-recorder-quota"]
    # The segment holding no whole frame is dropped; nothing was written anywhere else.
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert r.handle is None


@needs_select_on_pipes
def test_tc1395_an_interrupted_segment_is_cut_back_to_its_last_whole_frame(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok")] * 5, alerts)
    r.clock = lambda: 1000.0
    data = b"".join(stream(15)) + b"".join([pes(99 * 9000, False), body()])  # a frame cut short
    assert r.consume(FakeStream(data)) == 1  # EOF: the stream ended
    r.finalise()
    (seg,) = list((tmp_path / "door").rglob("door-*.ts"))
    raw = seg.read_bytes()
    assert len(raw) % 188 == 0
    last_pes = max(i for i in range(0, len(raw), 188)
                   if rec.packet_fields(raw[i:i + 188])[1] and rec.packet_fields(raw[i:i + 188])[0] == VIDEO_PID)
    # the file ends with the body of the last COMPLETE frame (2 body packets after its PES)
    assert len(raw) - last_pes == 3 * 188


@needs_select_on_pipes
def test_tc1395_a_cut_back_segment_keeps_the_time_of_its_last_write(tmp_path, monkeypatch):
    # A stall ends the run 30 s after the last data; the truncation must not stamp
    # the file with that moment (the clip cutter reads mtime as where the frames end).
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok")] * 5, alerts)
    r.clock = lambda: 1000.0
    monkeypatch.setattr(rec.time, "time", lambda: 1793000000.0)        # the wall clock while data arrives
    data = b"".join(stream(15)) + b"".join([pes(99 * 9000, False), body()])
    assert r.consume(FakeStream(data)) == 1
    monkeypatch.setattr(rec.time, "time", lambda: 1793000030.0)        # the stall, 30 s later
    r.finalise()
    (seg,) = list((tmp_path / "door").rglob("door-*.ts"))
    assert seg.stat().st_mtime == pytest.approx(1793000000.0, abs=1)


def test_tc1396_continuity_counts_gaps_over_tolerance():
    seg = [(0.0, 600.0), (600.5, 600.0), (1230.0, 570.0), (1800.0, 600.0)]
    c = rec.continuity(seg, 0.0, 2400.0)
    assert c["gaps"] == 1 and c["downtime_seconds"] == 29.5 and c["pass"]
    many = [(i * 700.0, 600.0) for i in range(6)]  # five 100 s gaps
    c = rec.continuity(many, 0.0, 4100.0)
    assert c["gaps"] == 5 and not c["pass"]
    long_gap = [(0.0, 600.0), (1000.0, 600.0)]
    assert not rec.continuity(long_gap, 0.0, 1600.0)["pass"]  # 400 s > 5 min


DAY = 86400.0


def test_tc1396_no_recording_or_a_short_one_never_passes_a_window():
    c = rec.continuity([], 0.0, DAY)
    assert not c["pass"] and c["segments"] == 0 and c["gap_list"][0]["kind"] == "empty"
    # even a window shorter than the downtime allowance: no segment is no pass
    assert not rec.continuity([], 0.0, 60.0)["pass"]
    # one ten-second segment at the end of a 24 h window: the rest is downtime
    c = rec.continuity([(DAY - 10.0, 10.0)], 0.0, DAY)
    assert not c["pass"] and c["gap_list"][0]["kind"] == "leading" and c["downtime_seconds"] == DAY - 10.0


def test_tc1396_both_edges_of_the_window_count():
    full = [(i * 600.0, 600.0) for i in range(144)]  # 24 h, back to back
    assert rec.continuity(full, 0.0, DAY)["pass"]
    # the recorder stopped 301 s before the check: a trailing gap over 300 s
    c = rec.continuity(full, 0.0, DAY + 301.0)
    assert not c["pass"] and c["gap_list"] == [{"kind": "trailing", "after": DAY, "seconds": 301.0}]
    # a late start likewise
    assert not rec.continuity(full[1:], 0.0, DAY)["pass"]  # 600 s before the first segment
    # the edge allowance (first keyframe, the open segment's last frame) is not downtime
    assert rec.continuity(full, -10.0, DAY + 10.0)["gaps"] == 0
    # a segment that began before the window covers its start (clipped, not a gap)
    assert rec.continuity(full, 300.0, DAY)["pass"]


def test_tc1396_the_verb_reads_the_window_and_the_open_part(tmp_path, capsys):
    t0 = 1792667254.0
    day, name = rec.segment_name(t0)
    ddir = tmp_path / day
    ddir.mkdir()
    (ddir / name).write_bytes(b"".join(stream(101)))  # 10.0 s
    part = ddir / (rec.segment_name(t0 + 10.0)[1] + ".part")
    part.write_bytes(b"".join(stream(101)))           # the segment being written, 10.0 s so far

    def verdict(*extra):
        code = rec.main(["continuity", "--root", str(tmp_path)] + list(extra))
        return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    code, c = verdict("--since", "%d" % t0, "--until", "2026-10-22T11:07:54Z")  # t0 + 20 s
    assert code == 0 and c["segments"] == 2 and c["gaps"] == 0
    part.unlink()
    code, c = verdict("--since", "%d" % t0, "--until", "%d" % (t0 + 40))
    assert c["gaps"] == 1 and c["downtime_seconds"] == 30.0  # 30 s uncovered at the end: counted
    assert c["gap_list"][-1]["kind"] == "trailing"
    code, c = verdict("--hours", "1", "--until", "%d" % (t0 + 10))  # 59m50s before the first segment
    assert code == 1 and c["gap_list"][0]["kind"] == "leading"


def test_tc1396_segment_names_are_utc_and_parse_back():
    day, name = rec.segment_name(1792667254.0)
    assert day == "2026-10-22" and name == "door-20261022T110734Z.ts"
    assert rec.segment_start(name) == 1792667254.0
    assert rec.segment_start("door-x.ts") is None


def test_tc1396_ts_duration_from_pts(tmp_path):
    f = tmp_path / "s.ts"
    f.write_bytes(b"".join(stream(101)))  # 10 fps, frames 0..100 -> 10 s
    assert rec.ts_duration(str(f)) == pytest.approx(10.0)


# ── TC-1399 the gate's verdicts: probe success, geometry, survivor progress ──


HEALTHY = {"rc": 0, "codec": "h264", "width": 1536, "height": 1536, "packets": 50, "rate": "10/1"}
# The relay's own sender counters RISE in every case below: go2rtc counts a
# packet when it is queued for a consumer, so they are no evidence of delivery.
PROGRESS = [{"consumers": 1, "sent_bytes": 1000}, {"consumers": 1, "sent_bytes": 9000}]
# The survivor's own -progress frame count, one entry per gate sleep (the two
# consumers' run, gate-2's departure, the progress window); None = no block.
RECEIVING = [10, 20, 30]


class FakeConsumer:
    """A gate consumer. The survivor's stdout is a real pipe the test writes
    ffmpeg `-progress` blocks into, exactly as ffmpeg would."""

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, **_kw):
        self.argv = list(argv)
        self.name = self.argv[self.argv.index("--name") + 1]
        self.stdin = FakeStdin() if stdin == dr.subprocess.PIPE else None
        self.stdout = self.writer = None
        if stdout == dr.subprocess.PIPE:
            r, w = os.pipe()
            self.stdout, self.writer = os.fdopen(r, "rb"), os.fdopen(w, "wb", buffering=0)

    def progress(self, frames):
        self.writer.write(("frame=%d\nfps=10.00\ntotal_size=N/A\nout_time_us=%d\nprogress=continue\n"
                           % (frames, frames * 100000)).encode())

    def poll(self):
        return None  # always "running": liveness alone must not be enough

    def wait(self, timeout=None):
        if self.writer is not None and not self.writer.closed:
            self.writer.close()
        return 0

    def kill(self):
        self.wait()


REAL_PROGRESS_READER = dr.ProgressReader


def run_gate(monkeypatch, tmp_path, capsys, probe_result=None, counters=None, frames=None, docker=None, **env):
    """The gate on fakes. With `docker` (a FakeDocker) the REAL probe runs
    against it, and every `docker run`/`docker rm` goes through it."""
    import time as real_time
    real_sleep = real_time.sleep
    s = dr.RelaySettings(write_env(tmp_path, **env))
    if docker is None:
        monkeypatch.setattr(dr, "probe", lambda *a, **k: dict(probe_result or HEALTHY))
    monkeypatch.setattr(dr, "upstream_count", lambda _s: 1)
    monkeypatch.setattr(dr, "streams", lambda _s: {"producers": 1, "consumers": 2})
    seq = iter(PROGRESS if counters is None else counters)
    monkeypatch.setattr(dr, "consumer_bytes", lambda _s: next(seq))
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    consumers, readers = [], []

    def popen(*a, **k):
        consumers.append(FakeConsumer(*a, **k))
        return consumers[-1]

    now = [1000.0]  # the gate's clock: advanced only by its own (fake) sleeps

    class Reader(REAL_PROGRESS_READER):  # not dr.ProgressReader: a second run_gate in one test patched it
        def __init__(self, stream):
            super().__init__(stream, clock=lambda: now[0])
            readers.append(self)
    monkeypatch.setattr(dr, "ProgressReader", Reader)
    monkeypatch.setattr(dr.subprocess, "Popen", popen)
    monkeypatch.setattr(dr.subprocess, "run", docker.run if docker else (lambda *a, **k: dr.subprocess.CompletedProcess(a[0], 0, None, b"")))
    script = iter(RECEIVING if frames is None else frames)
    written = [0]

    def report(count):
        (survivor,) = [c for c in consumers if c.writer is not None]
        survivor.progress(count)
        written[0] += 5
        deadline = real_time.monotonic() + 5  # let the reader thread catch up: deterministic
        while readers[0].lines < written[0] and real_time.monotonic() < deadline:
            real_sleep(0.005)

    def sleep(seconds):
        # Each gate sleep is a stretch in which the survivor reports (or not).
        # An int is a block at the END of the stretch (it kept receiving); an
        # ("early", n) a block at its START and nothing after (it stopped).
        step = next(script, None)
        if isinstance(step, tuple):
            report(step[1])
        now[0] += seconds
        if isinstance(step, int):
            report(step)
    monkeypatch.setattr(dr.time, "sleep", sleep)
    code = dr.gate(s, 1)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert lines[-1]["check"] == "gate-hub-half" and lines[-1]["pass"] is (code == 0)
    checks = {l["check"]: l for l in lines}
    checks["_consumers"] = consumers
    return code, checks


def test_tc1399_a_healthy_relay_passes_the_hub_half(monkeypatch, tmp_path, capsys):
    code, checks = run_gate(monkeypatch, tmp_path, capsys)
    left = checks["one-consumer-left"]
    assert code == 0 and left["survivor_progressing"] and left["survivor_frames"] == [20, 30]
    assert left["survivor_last_frame_age"] == 0.0
    assert left["relay_sent_bytes"] == [1000, 9000]  # reported, not the verdict
    assert checks["readiness+geometry"]["reasons"] == []


def test_tc1399_the_gate_consumers_report_progress_and_carry_no_secret(monkeypatch, tmp_path, capsys):
    s = dr.RelaySettings(write_env(tmp_path))
    _code, checks = run_gate(monkeypatch, tmp_path, capsys)
    by_name = {c.name: c for c in checks["_consumers"]}
    assert set(by_name) == {"homehub-door-gate-2", "homehub-door-gate-3"}
    for c in by_name.values():
        assert_consumer_started_by_stdin(c, s)
    survivor = by_name["homehub-door-gate-3"].argv
    assert survivor[survivor.index("-progress") + 1] == "pipe:1"
    assert "-progress" not in by_name["homehub-door-gate-2"].argv


@pytest.mark.parametrize("frames", [
    [10, 20, 20],      # ffmpeg alive and reporting, but its frame count flat: nothing received
    [10, 20, None],    # no block at all in the window: the consumer is stopped (SIGSTOP) or wedged
    [None, None, None],  # never reported
    [10, 20, ("early", 25)],  # a few frames just after the first sample, then stopped (the review's timing)
])
def test_tc1399_relay_counters_rising_while_the_survivor_receives_nothing_fails(monkeypatch, tmp_path, capsys, frames):
    # Sol's reproduction: gate-3 SIGSTOPped after the first sample; the relay's
    # sender bytes kept rising (PROGRESS) and the old gate passed.
    code, checks = run_gate(monkeypatch, tmp_path, capsys, frames=frames)
    left = checks["one-consumer-left"]
    assert left["relay_sent_bytes"][1] > left["relay_sent_bytes"][0]
    assert left["survivor_running"] is True
    assert code == 1 and left["survivor_progressing"] is False


def test_tc1399_the_progress_reader_counts_only_complete_blocks():
    t = [50.0]
    r = dr.ProgressReader(io.BytesIO(b""), clock=lambda: t[0])
    r.thread.join(5)
    assert r.stalled_for() is None
    for line in ("frame=5", "fps=10", "progress=continue", "frame=9"):
        r.feed(line + "\n")
    assert r.frames == 5 and r.blocks == 1  # frame=9 has no progress= line yet
    t[0] = 51.0
    r.feed("progress=end\n")
    assert r.frames == 9 and r.advanced_at == 51.0
    t[0] = 54.0
    for line in ("frame=9", "progress=continue", "frame=N/A", "progress=continue"):
        r.feed(line + "\n")
    assert r.frames == 9 and r.stalled_for() == 3.0  # a repeated count is not a rise


@pytest.mark.parametrize("bad, words", [
    ({"rc": 1}, "probe exited 1"),
    ({"rc": 1, "packets": 1, "width": 320, "height": 240}, "geometry 320x240"),  # the review's own case
    ({"width": 1280, "height": 720}, "expected 1536x1536"),
    ({"codec": "hevc"}, "codec 'hevc'"),
    ({"packets": 0}, "no packets"),
])
def test_tc1399_a_failed_probe_or_altered_stream_fails_the_gate(monkeypatch, tmp_path, capsys, bad, words):
    code, checks = run_gate(monkeypatch, tmp_path, capsys, probe_result=dict(HEALTHY, **bad))
    assert code == 1
    assert any(words in r for r in checks["readiness+geometry"]["reasons"]), checks["readiness+geometry"]


def test_tc1399_the_expected_stream_is_a_knob(monkeypatch, tmp_path, capsys):
    code, _ = run_gate(monkeypatch, tmp_path, capsys, probe_result=dict(HEALTHY, codec="hevc", width=960, height=960),
                       DOOR_RELAY_EXPECT_CODEC="hevc", DOOR_RELAY_EXPECT_SIZE="960x960")
    assert code == 0
    with pytest.raises(dr.Invalid, match="DOOR_RELAY_EXPECT_SIZE"):
        dr.RelaySettings(write_env(tmp_path, DOOR_RELAY_EXPECT_SIZE="big"))


@pytest.mark.parametrize("counters", [
    [{"consumers": 1, "sent_bytes": 5000}, {"consumers": 1, "sent_bytes": 5000}],  # frozen
    [None, None],                                                                    # API silent
])
def test_tc1399_the_relays_counters_decide_nothing_the_consumers_frames_do(monkeypatch, tmp_path, capsys, counters):
    # The secondary field may be flat or missing; a survivor whose own frame
    # count rises is receiving video, and that is the verdict.
    code, checks = run_gate(monkeypatch, tmp_path, capsys, counters=counters)
    assert code == 0 and checks["one-consumer-left"]["survivor_progressing"] is True


def test_tc1399_the_probe_verb_uses_the_same_verdict(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dr, "probe", lambda *a, **k: dict(HEALTHY, width=320, height=240))
    assert dr.main(["--env-file", write_env(tmp_path), "probe", "--seconds", "1"]) == 1
    monkeypatch.setattr(dr, "probe", lambda *a, **k: dict(HEALTHY))
    assert dr.main(["--env-file", write_env(tmp_path), "probe", "--seconds", "1"]) == 0


def test_tc1399_consumer_bytes_reads_the_senders_counters_of_the_pinned_api(monkeypatch, tmp_path):
    # The shape go2rtc 1.9.14 returned in the lab for one RTSP consumer (trimmed):
    # no connection-level counter; each sender carries its own bytes/packets.
    s = dr.RelaySettings(write_env(tmp_path))
    entry = {"producers": [{"bytes_recv": 287315}],
             "consumers": [{"format_name": "rtsp", "senders": [{"id": 4, "parent": 3, "bytes": 284579, "packets": 228}]},
                           {"format_name": "rtsp", "senders": [{"id": 6, "bytes": 1000}, {"id": 7}]}]}
    monkeypatch.setattr(dr, "api_stream", lambda _s: entry)
    assert dr.consumer_bytes(s) == {"consumers": 2, "sent_bytes": 285579}

    def down(_s):
        raise OSError("connection refused")
    monkeypatch.setattr(dr, "api_stream", down)
    assert dr.consumer_bytes(s) is None


# ── TC-1399 a probe that never finishes leaves no consumer behind (review F8) ─
#
# Killing the docker CLI does not stop its container: --rm removes it only
# after ffprobe exits, and against a camera that accepts TCP but never answers
# RTSP that is never. Measured against the pinned image: the container still
# running after the client's timeout, and a retry refused with a name conflict.


class FakeDocker:
    """The docker CLI as a stateful fake. `docker run --name X` is refused
    (rc 125, name conflict) while a container X exists. A run named in `stall`
    (once) or `interrupt` (once) loses its CLI to the deadline or to Ctrl-C
    and LEAVES its container running, as the real one does, until
    `docker rm -f X`."""

    def __init__(self, stall=(), interrupt=()):
        self.running, self.calls = set(), []
        self.stall, self.interrupt = set(stall), set(interrupt)

    def run(self, argv, input=None, stdout=None, stderr=None, timeout=None, **_kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["docker", "rm", "-f"]:
            self.running.difference_update(argv[3:])
            return dr.subprocess.CompletedProcess(argv, 0, None, None)
        assert argv[:2] == ["docker", "run"] and timeout is not None
        name = argv[argv.index("--name") + 1]
        if name in self.running:
            return dr.subprocess.CompletedProcess(
                argv, 125, b"", b'docker: Error response from daemon: Conflict. The container name "/%s" '
                                b'is already in use by container "0f".' % name.encode())
        if name in self.stall | self.interrupt:
            self.running.add(name)  # the client dies; the container does not
            if name in self.stall:
                self.stall.discard(name)
                partial = ("[rtsp @ 0x5] method DESCRIBE failed for %s" % input.decode().split("'")[1]).encode()
                raise dr.subprocess.TimeoutExpired(argv, timeout, output=b"", stderr=partial)
            self.interrupt.discard(name)
            raise KeyboardInterrupt
        # ffprobe ran to the end and exited; --rm removed its container.
        out = json.dumps({"streams": [{"codec_name": "h264", "width": 1536, "height": 1536,
                                       "nb_read_packets": "51", "avg_frame_rate": "10/1"}]})
        return dr.subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    def removed(self, name):
        return [c for c in self.calls if c[:3] == ["docker", "rm", "-f"] and name in c[3:]]


def test_tc1399_a_probe_timeout_removes_its_container_and_a_retry_succeeds(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    docker = FakeDocker(stall={"homehub-door-probe"})
    monkeypatch.setattr(dr.subprocess, "run", docker.run)
    result = dr.probe(s, 7)
    assert result["timeout"] is True and result["rc"] == -1 and result["timeout_sec"] == 67
    assert docker.removed("homehub-door-probe") and docker.running == set()
    reasons = dr.probe_verdict(result, s)
    assert any("timed out after 67 s" in r for r in reasons), reasons
    blob = json.dumps(result)
    assert "rtsp://***@" in blob  # the partial stderr is kept, redacted
    for form in dr.secret_forms(s) + [s.consumer_url()]:
        assert form not in blob
    # The retry under the SAME fixed name is not refused.
    again = dr.probe(s, 7)
    assert again["rc"] == 0 and dr.probe_verdict(again, s) == []


def test_tc1399_an_interrupted_probe_removes_its_container_and_re_raises(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    docker = FakeDocker(interrupt={"homehub-door-probe"})
    monkeypatch.setattr(dr.subprocess, "run", docker.run)
    with pytest.raises(KeyboardInterrupt):
        dr.probe(s, 7)
    assert docker.removed("homehub-door-probe") and docker.running == set()
    assert dr.probe_verdict(dr.probe(s, 7), s) == []


def test_tc1399_the_probe_verb_fails_a_timeout_and_its_retry_passes(monkeypatch, tmp_path, capsys):
    docker = FakeDocker(stall={"homehub-door-probe"})
    monkeypatch.setattr(dr.subprocess, "run", docker.run)
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    env = write_env(tmp_path)
    assert dr.main(["--env-file", env, "probe", "--seconds", "1"]) == 1
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["pass"] is False and first["timeout"] is True
    assert dr.main(["--env-file", env, "probe", "--seconds", "1"]) == 0


def test_tc1399_a_stalled_gate_probe_leaves_no_consumer_and_the_gate_reruns(monkeypatch, tmp_path, capsys):
    docker = FakeDocker(stall={"homehub-door-gate-1"})
    code, checks = run_gate(monkeypatch, tmp_path, capsys, docker=docker)
    assert code == 1 and checks["readiness+geometry"]["timeout"] is True
    assert any("timed out" in r for r in checks["readiness+geometry"]["reasons"])
    assert docker.removed("homehub-door-gate-1") and "homehub-door-gate-1" not in docker.running
    code, checks = run_gate(monkeypatch, tmp_path, capsys, docker=docker)
    assert code == 0 and checks["readiness+geometry"]["reasons"] == []


def test_tc1399_a_gate_interrupted_in_its_probe_removes_gate_1(monkeypatch, tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    docker = FakeDocker(interrupt={"homehub-door-gate-1"})
    started = []
    monkeypatch.setattr(dr.subprocess, "run", docker.run)
    monkeypatch.setattr(dr.subprocess, "Popen", lambda *a, **k: started.append(a) or FakeConsumer(*a, **k))
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    with pytest.raises(KeyboardInterrupt):
        dr.gate(s, 1)
    assert docker.removed("homehub-door-gate-1") and docker.running == set()
    assert started == []  # nothing else was started to be left behind


# ── TC-1399 a SIGTERM or SIGHUP still removes the consumers (review round 4 F8) ─
#
# Python's default SIGTERM/SIGHUP action ends the process without unwinding, so
# neither probe()'s except nor gate()'s and cost's finally ran: Sol reproduced
# `python_rc=-15 container_running=true` for probe and gate. These run the REAL
# CLI as a child process, against a silent TCP fixture standing in for the
# relay, with a stub docker on PATH whose containers - like the real ones -
# outlive their killed client and hold their session until `docker rm -f`.

STUB_DOCKER = r'''#!{python}
import json, os, re, subprocess, sys, time
state = os.environ["FAKE_DOCKER_STATE"]
argv = sys.argv[1:]
with open(os.path.join(state, "calls.log"), "a") as log:
    log.write(json.dumps(argv) + "\n")
def box(name):
    return os.path.join(state, "c-" + name)
def event(what):
    with open(os.path.join(state, "rm-events.log"), "a") as log:
        log.write(what + " " + " ".join(argv[2:]) + "\n")
if argv[:2] == ["rm", "-f"]:
    # FAKE_DOCKER_RM_SLOW: a removal that takes that long (a loaded daemon),
    # so a signal can be sent while it is under way. The events say whether it
    # started and whether it ran to its end.
    event("start")
    time.sleep(float(os.environ.get("FAKE_DOCKER_RM_SLOW") or 0))
    for name in argv[2:]:
        try:
            pid = int(open(box(name)).read())
            os.kill(pid, 9)
        except (OSError, ValueError):
            pass
        try:
            os.unlink(box(name))
        except OSError:
            pass
    event("finish")
    sys.exit(0)
if argv[:1] == ["inspect"]:
    sys.exit(1)
assert argv[0] == "run", argv
name = argv[argv.index("--name") + 1]
if os.path.exists(box(name)):
    sys.stderr.write('docker: Error response from daemon: Conflict. The container name "/%s" is already in use\n' % name)
    sys.exit(125)
document = sys.stdin.buffer.read().decode()  # the consumer's input document; stdin is closed after it
host, port = re.search(r"@([0-9.]+):([0-9]+)/", document).groups()
if name in os.environ.get("FAKE_DOCKER_FINISH", "").split(","):
    # ffprobe read its seconds and exited; --rm removed the container.
    print(json.dumps({{"streams": [{{"codec_name": "h264", "width": 1536, "height": 1536,
                                    "nb_read_packets": "51", "avg_frame_rate": "10/1"}}]}}))
    sys.exit(0)
# The container: its own session, so killing this client leaves it holding
# its connection to the relay - which never answers - until `docker rm -f`.
holder = (
    "import os, socket, sys, time\n"
    "s = socket.create_connection((sys.argv[1], int(sys.argv[2])))\n"
    "open(sys.argv[3] + '.tmp', 'w').write(str(os.getpid()))\n"
    "os.rename(sys.argv[3] + '.tmp', sys.argv[3])\n"
    "time.sleep(3600)\n")
subprocess.Popen([sys.executable, "-c", holder, host, port, box(name)], start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
t0 = time.monotonic()
while not os.path.exists(box(name)) and time.monotonic() - t0 < 10:
    time.sleep(0.05)
while os.path.exists(box(name)):  # attached, as `docker run` is, until the container goes
    time.sleep(0.05)
sys.exit(137)
'''


class SilentRelay:
    """A TCP listener that accepts and never answers (a relay or camera that
    took the connection and says nothing). `sessions_open()` counts accepted
    connections the far side has not closed."""

    def __init__(self):
        import socket
        import threading
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.accepted = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            conn.setblocking(False)
            self.accepted.append(conn)

    def sessions_open(self):
        n = 0
        for conn in self.accepted:
            try:
                if conn.recv(1) != b"":
                    n += 1
            except BlockingIOError:
                n += 1
            except OSError:
                pass
        return n

    def close(self):
        for conn in self.accepted:
            conn.close()
        self.server.close()


@pytest.fixture
def cli_rig(tmp_path):
    """(run, relay, signal): `run(args, wait_for, sig, finish)` starts the real
    door_relay.py CLI against the stub docker and the silent relay, waits until
    every `wait_for` container holds its session, sends `sig` (None: none),
    and returns the CLI's exit status and output."""
    import signal as sg
    import subprocess as sp
    import time as tm
    relay = SilentRelay()
    state = tmp_path / "docker-state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(STUB_DOCKER.format(python=sys.executable))
    stub.chmod(0o755)
    env_file = write_env(tmp_path, LAN_IP="127.0.0.1", DOOR_RELAY_RTSP_PORT=str(relay.port))
    procs = []

    def run(args, wait_for, sig, finish=(), rm_slow=0, in_removal=False, grace=None, via=None):
        """`in_removal`: signal once a `docker rm -f` has STARTED (and is held
        for `rm_slow` s), not when the consumers run. `grace`: the probe's
        PROBE_GRACE_SEC, so its timeout path is reached in seconds. `via`:
        None sends `sig` to the CLI alone; "group" starts the CLI as a process
        group leader and sends it to the whole group (os.killpg - Ctrl-C at
        a terminal); "timeout" runs the CLI under GNU `timeout` and sends it
        to timeout, which passes it to the CLI AND to its process group."""
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_DOCKER_STATE=str(state), FAKE_DOCKER_FINISH=",".join(finish),
                   FAKE_DOCKER_RM_SLOW=str(rm_slow),
                   HOMEHUB_MONITOR_OVERRIDE_FILE=str(tmp_path / "no-override"))
        cli = [str(STACK / "door" / "door_relay.py")]
        if grace is not None or via:
            # The same main(). A grace shortens one constant. For a group
            # signal, Ctrl-C is armed as an interactive Python has it, even
            # when this test runs with SIGINT ignored (a background job).
            cli = ["-c", "import signal, sys; sys.path.insert(0, %r); import door_relay as d; "
                         "signal.signal(signal.SIGINT, signal.default_int_handler); %s"
                         "sys.exit(d.main(sys.argv[1:]))"
                         % (str(STACK / "door"), "" if grace is None else "d.PROBE_GRACE_SEC = %d; " % grace)]
        prefix = ["timeout", "600"] if via == "timeout" else []
        proc = sp.Popen(prefix + [sys.executable] + cli + ["--env-file", env_file] + list(args),
                        stdout=sp.PIPE, stderr=sp.STDOUT, env=env, start_new_session=(via == "group"))
        procs.append(proc)
        if sig is not None:
            deadline = tm.monotonic() + 60
            while not (rm_events("start") if in_removal else
                       all((state / ("c-" + n)).exists() for n in wait_for)):
                assert proc.poll() is None, proc.stdout.read().decode()
                assert tm.monotonic() < deadline, "the consumers never started"
                tm.sleep(0.05)
            tm.sleep(0.3)  # into the wait the verb is making, not the instant of the start
            if via == "group":
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        out = proc.communicate(timeout=90)[0].decode()
        return proc.returncode, out

    def rm_events(kind):
        log = state / "rm-events.log"
        if not log.exists():
            return []
        return [line for line in log.read_text().splitlines() if line.startswith(kind + " ")]

    def calls():
        log = state / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def sessions_closed():
        deadline = tm.monotonic() + 5
        while relay.sessions_open() and tm.monotonic() < deadline:
            tm.sleep(0.05)
        return relay.sessions_open() == 0

    run.calls = calls
    run.containers = lambda: sorted(p.name[2:] for p in state.glob("c-*") if not p.name.endswith(".tmp"))
    run.sessions_closed = sessions_closed
    run.rm_events = rm_events
    try:
        yield run, relay, sg
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        for box in state.glob("c-*"):  # a failing case must not leave holders running
            try:
                os.kill(int(box.read_text()), sg.SIGKILL)
            except (OSError, ValueError):
                pass
        relay.close()


def removed(calls, name):
    return any(c[:2] == ["rm", "-f"] and name in c[2:] for c in calls)


posix_signals = pytest.mark.skipif(sys.platform.startswith("win"),
                                   reason="SIGTERM/SIGHUP to a child process and a stub docker are POSIX")


@posix_signals
@pytest.mark.parametrize("signame,code", [("SIGTERM", 143), ("SIGHUP", 129)])
def test_tc1399_a_signalled_probe_removes_its_container_and_a_retry_passes(cli_rig, signame, code):
    run, relay, sg = cli_rig
    rc, out = run(["probe", "--seconds", "30"], ["homehub-door-probe"], getattr(sg, signame))
    assert rc == code, out
    assert removed(run.calls(), "homehub-door-probe"), run.calls()
    assert run.containers() == [] and relay.accepted and run.sessions_closed()
    assert "stopped by signal %d" % getattr(sg, signame) in out
    # The fixed name is free again: the retry is not refused.
    rc, out = run(["probe", "--seconds", "1"], [], None, finish=["homehub-door-probe"])
    assert rc == 0, out
    assert json.loads(out.strip().splitlines()[-1])["pass"] is True


@posix_signals
def test_tc1399_a_gate_signalled_in_its_probe_removes_gate_1(cli_rig):
    run, relay, sg = cli_rig
    rc, out = run(["gate", "--seconds", "30"], ["homehub-door-gate-1"], sg.SIGTERM)
    assert rc == 143, out
    assert removed(run.calls(), "homehub-door-gate-1")
    assert run.containers() == [] and relay.accepted and run.sessions_closed()


@posix_signals
def test_tc1399_a_gate_signalled_with_two_consumers_removes_both(cli_rig):
    run, relay, sg = cli_rig
    rc, out = run(["gate", "--seconds", "30"], ["homehub-door-gate-2", "homehub-door-gate-3"], sg.SIGTERM,
                  finish=["homehub-door-gate-1"])
    assert rc == 143, out
    calls = run.calls()
    assert removed(calls, "homehub-door-gate-2") and removed(calls, "homehub-door-gate-3")
    assert run.containers() == [] and len(relay.accepted) == 2 and run.sessions_closed()
    assert '"gate-hub-half"' not in out  # a stopped gate reports no verdict


@posix_signals
def test_tc1399_a_signalled_cost_run_removes_its_grey_decoder(cli_rig):
    run, relay, sg = cli_rig
    rc, out = run(["cost", "--seconds", "60", "--decode-grey-fps", "5"], [dr.GREY_DECODER], sg.SIGTERM)
    assert rc == 143, out
    assert removed(run.calls(), dr.GREY_DECODER)
    assert run.containers() == [] and relay.accepted and run.sessions_closed()


@posix_signals
def test_tc1399_the_verbs_restore_the_signal_handlers_they_found(monkeypatch, tmp_path, capsys):
    import signal as sg

    def mine(*_a):
        pass
    before = {sig: sg.signal(sig, mine) for sig in (sg.SIGTERM, sg.SIGHUP)}
    try:
        monkeypatch.setattr(dr.subprocess, "run", FakeDocker().run)
        monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
        assert dr.main(["--env-file", write_env(tmp_path), "probe", "--seconds", "1"]) == 0
        assert sg.getsignal(sg.SIGTERM) is mine and sg.getsignal(sg.SIGHUP) is mine
        with dr.stop_signals_raise():
            with pytest.raises(dr.Stopped) as stopped:
                os.kill(os.getpid(), sg.SIGTERM)
            assert stopped.value.code == 143
            # The cleanup the first signal started is not cut short by a second.
            os.kill(os.getpid(), sg.SIGHUP)
        assert sg.getsignal(sg.SIGTERM) is mine and sg.getsignal(sg.SIGHUP) is mine
    finally:
        for sig, handler in before.items():
            sg.signal(sig, handler)


# ── TC-1399 the FIRST signal landing in a removal does not abandon it (round 5 F8)
#
# Round 4 raised Stopped on the first signal wherever it landed, including
# inside the `docker rm -f` of a cleanup entered on a timeout or a normal
# finish: the removal was killed, the container kept its session and its name,
# and main() still logged "its consumers were removed". Sol: `normal cost
# cleanup SIGTERM: rc=143 holder_still_running=True / stop line claims removed:
# True / fixture sessions still open: 1`. The stub's `rm -f` is held for 3 s
# and the signal is sent once it has started.


@posix_signals
def test_tc1399_a_signal_during_the_cost_runs_final_removal_waits_for_it(cli_rig):
    run, relay, sg = cli_rig
    rc, out = run(["cost", "--seconds", "1", "--decode-grey-fps", "5"], [], sg.SIGTERM,
                  rm_slow=3, in_removal=True)
    assert rc == 143, out
    assert run.rm_events("finish") == ["finish " + dr.GREY_DECODER], (run.rm_events("start"), out)
    assert run.containers() == [] and relay.accepted and run.sessions_closed()
    assert '"seconds"' in out  # the measurement itself had finished and was printed
    assert "stopped by signal 15; its consumers were removed (%s)" % dr.GREY_DECODER in out, out


@posix_signals
def test_tc1399_a_signal_during_the_probe_timeouts_removal_waits_for_it_and_still_stops(cli_rig):
    run, relay, sg = cli_rig
    rc, out = run(["probe", "--seconds", "1"], [], sg.SIGTERM, rm_slow=3, in_removal=True, grace=2)
    assert rc == 143, out  # the deferred signal is raised, not lost by the timeout branch's return
    assert run.rm_events("finish") == ["finish homehub-door-probe"], (run.rm_events("start"), out)
    assert run.containers() == [] and relay.accepted and run.sessions_closed()
    assert '"pass"' not in out  # stopped: no verdict printed
    assert "stopped by signal 15; its consumers were removed (homehub-door-probe)" in out, out


@posix_signals
def test_tc1399_cleanup_defers_a_signal_until_the_outermost_block_ends(monkeypatch):
    import signal as sg
    ran = []
    with dr.stop_signals_raise():
        with pytest.raises(dr.Stopped) as stopped:
            with dr.defer_stop():
                with dr.defer_stop():
                    os.kill(os.getpid(), sg.SIGHUP)
                    ran.append("inner")  # not cut short
                ran.append("outer")      # the inner block's end does not raise
                os.kill(os.getpid(), sg.SIGTERM)  # a later signal is swallowed
                ran.append("end")
        assert stopped.value.signum == sg.SIGHUP and stopped.value.code == 129
        os.kill(os.getpid(), sg.SIGTERM)  # the stop is under way: swallowed
    assert ran == ["inner", "outer", "end"]
    with dr.defer_stop():  # outside the verbs it is inert
        pass


@posix_signals
@pytest.mark.parametrize("rm_outcome", ["timeout", "daemon-error"])
def test_tc1399_the_stop_line_says_when_a_removal_was_not_confirmed(monkeypatch, tmp_path, capsys, rm_outcome):
    import signal as sg
    calls = []

    def run(argv, input=None, stdout=None, stderr=None, timeout=None, **_kw):
        argv = list(argv)
        calls.append(argv)
        if argv[:3] == ["docker", "rm", "-f"]:
            if rm_outcome == "timeout":
                raise dr.subprocess.TimeoutExpired(argv, timeout)
            return dr.subprocess.CompletedProcess(argv, 1, None, b"Error response from daemon: busy\n")
        os.kill(os.getpid(), sg.SIGTERM)  # the signal lands while the probe runs
        raise AssertionError("not reached: the handler raises")
    monkeypatch.setattr(dr.subprocess, "run", run)
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    assert dr.main(["--env-file", write_env(tmp_path), "probe", "--seconds", "1"]) == 143
    out = capsys.readouterr().out
    assert ["docker", "rm", "-f", "homehub-door-probe"] in calls
    assert "removal NOT confirmed for homehub-door-probe" in out, out
    assert "were removed" not in out


def test_tc1399_a_removal_confirms_only_what_docker_finished(monkeypatch):
    dr.CONSUMERS.reset()
    for name in ("homehub-door-gate-1", "homehub-door-gate-2"):
        dr.CONSUMERS.start(name)
    outcomes = iter([dr.subprocess.CompletedProcess([], 0, None, b"Error response from daemon: No such container: "
                                                                  b"homehub-door-gate-1\n"),
                     dr.subprocess.CompletedProcess([], 1, None, b"Error response from daemon: No such container: "
                                                                  b"homehub-door-gate-1\n")])
    monkeypatch.setattr(dr.subprocess, "run", lambda *a, **k: next(outcomes))
    assert dr.remove_containers("homehub-door-gate-1") is True  # already gone counts as removed
    assert dr.remove_containers("homehub-door-gate-1") is True  # older docker: rc 1, only "No such container"
    assert dr.CONSUMERS.unconfirmed() == ["homehub-door-gate-2"]
    assert "removal NOT confirmed for homehub-door-gate-2" in dr.stop_line("gate", 15)
    monkeypatch.setattr(dr.subprocess, "run", lambda *a, **k: dr.subprocess.CompletedProcess([], 0, None, b""))
    assert dr.remove_containers("homehub-door-gate-2") is True
    assert dr.stop_line("gate", 15).endswith("its consumers were removed (homehub-door-gate-1 homehub-door-gate-2)")
    dr.CONSUMERS.reset()
    assert dr.stop_line("cost", 1).endswith("it had started no consumer")


# ── TC-1399 a removal killed with the caller's process group (round 6 F8)
#
# defer_stop() shields the Python process only. `docker rm -f` ran in the
# caller's process group, so a group-wide signal - Ctrl-C at an ssh/sudo
# terminal, GNU `timeout` without --foreground - killed the removal too, and
# `if done.returncode and errors` then CONFIRMED it: a signalled rm has a
# nonzero rc and an empty stderr. Sol: `real Docker / GNU timeout: rc=143
# container_running=true / rm events: start / stop line: ... its consumers
# were removed`.


@pytest.mark.parametrize("rc,stderr", [
    (-15, b""),                                # killed by SIGTERM
    (-2, b""),                                 # killed by SIGINT
    (143, b""),                                # 128+15 through a wrapper (sh, timeout)
    (130, b""),                                # 128+2 through a wrapper
    (1, b""),                                  # failed, said nothing
    (1, b"Error response from daemon: No such container: x\nError response from daemon: busy\n"),
    (0, b"Error response from daemon: busy\n"),  # rc 0 with an error beside it
])
def test_tc1399_a_removal_that_did_not_finish_cleanly_is_not_confirmed(monkeypatch, rc, stderr):
    dr.CONSUMERS.reset()
    dr.CONSUMERS.start("homehub-door-probe")
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return dr.subprocess.CompletedProcess(argv, rc, None, stderr)
    monkeypatch.setattr(dr.subprocess, "run", run)
    assert dr.remove_containers("homehub-door-probe") is False
    assert "homehub-door-probe" not in dr.CONSUMERS.removed
    assert "removal NOT confirmed for homehub-door-probe" in dr.stop_line("probe", 15)
    assert seen.get("start_new_session") is True  # out of the caller's process group
    dr.CONSUMERS.reset()


@posix_signals
def test_tc1399_ctrl_c_is_deferred_like_sigterm_and_an_ignored_sigint_is_left_alone():
    """SIGINT with Python's own handler becomes Stopped(130), which a cleanup
    block defers - a KeyboardInterrupt could not be deferred, and inside
    subprocess.run it kills the removal. An ignored SIGINT stays ignored."""
    import signal as sg
    before = sg.signal(sg.SIGINT, sg.default_int_handler)
    try:
        ran = []
        with dr.stop_signals_raise():
            assert sg.getsignal(sg.SIGINT) is not sg.default_int_handler
            with pytest.raises(dr.Stopped) as stopped:
                with dr.defer_stop():
                    os.kill(os.getpid(), sg.SIGINT)
                    ran.append("cleanup")  # not cut short by a KeyboardInterrupt
            assert stopped.value.signum == sg.SIGINT and stopped.value.code == 130
        assert ran == ["cleanup"]
        assert sg.getsignal(sg.SIGINT) is sg.default_int_handler
        sg.signal(sg.SIGINT, sg.SIG_IGN)
        with dr.stop_signals_raise():
            assert sg.getsignal(sg.SIGINT) is sg.SIG_IGN
        assert sg.getsignal(sg.SIGINT) is sg.SIG_IGN
    finally:
        sg.signal(sg.SIGINT, before)


@posix_signals
@pytest.mark.parametrize("via", ["group", "timeout"])
@pytest.mark.parametrize("signame,code", [("SIGTERM", 143), ("SIGINT", 130)])
@pytest.mark.parametrize("verb", ["cost", "probe-timeout"])
def test_tc1399_a_signal_to_the_whole_process_group_cannot_kill_the_removal(cli_rig, verb, signame, code, via):
    """The removal is held 3 s by the stub; the signal goes to the CLI's
    whole process group once it has started. The rm must run to its end and
    the stop line must report what it confirmed."""
    import shutil
    if via == "timeout" and not shutil.which("timeout"):
        pytest.skip("GNU timeout is not on PATH")
    run, relay, sg = cli_rig
    sig = getattr(sg, signame)
    if verb == "cost":
        args, kw, name = ["cost", "--seconds", "1", "--decode-grey-fps", "5"], {}, dr.GREY_DECODER
    else:
        args, kw, name = ["probe", "--seconds", "1"], {"grace": 2}, "homehub-door-probe"
    rc, out = run(args, [], sig, rm_slow=3, in_removal=True, via=via, **kw)
    assert run.rm_events("finish") == ["finish " + name], (run.rm_events("start"), out)
    assert rc == code, out
    assert run.containers() == [] and relay.accepted and run.sessions_closed()
    assert "stopped by signal %d; its consumers were removed (%s)" % (sig, name) in out, out
    if verb == "probe-timeout":
        assert '"pass"' not in out  # stopped: no verdict printed


@posix_signals
def test_tc1399_a_signal_in_the_gates_cleanup_waits_for_every_consumer(monkeypatch, tmp_path):
    """gate()'s whole finally is one defer_stop() block: a signal landing in
    its first wait still lets the second consumer be waited for (killed on
    its deadline), and Stopped comes after."""
    import signal as sg
    s = dr.RelaySettings(write_env(tmp_path))
    waited, calls = [], []

    class Consumer:
        def __init__(self, name):
            self.name, self.stdout = name, io.BytesIO(b"")

        def wait(self, timeout=None):
            waited.append(self.name)
            if len(waited) == 1:
                os.kill(os.getpid(), sg.SIGTERM)  # lands in the cleanup
            return 0

        def kill(self):
            pass

    def start(argv, payload, stdout=None, stderr=None):
        return Consumer(argv[argv.index("--name") + 1])

    def boom(_seconds):
        raise RuntimeError("the gate failed while both consumers ran")
    monkeypatch.setattr(dr, "probe", lambda *a, **k: dict(HEALTHY))
    monkeypatch.setattr(dr, "start_consumer", start)
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    monkeypatch.setattr(dr.time, "sleep", boom)
    monkeypatch.setattr(dr.subprocess, "run",
                        lambda argv, **k: calls.append(list(argv)) or dr.subprocess.CompletedProcess(argv, 0, None, b""))
    with dr.stop_signals_raise():
        with pytest.raises(dr.Stopped) as stopped:
            dr.gate(s, 1)
    assert stopped.value.code == 143
    assert waited == ["homehub-door-gate-2", "homehub-door-gate-3"]
    assert ["docker", "rm", "-f", "homehub-door-gate-1", "homehub-door-gate-2", "homehub-door-gate-3"] in calls
