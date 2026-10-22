"""The door relay's host side and the recorder's pure core (SN-050, SR-105..SR-107).

What runs here, on the dev PC: the relay config (every listener named, auth on
both, video only, the credential encoded), the field validation that names keys
and never values, the panel-to-hub credential import, the pin agreeing with the
compose image, the unit/compose/firstboot contract, and the recorder's MPEG-TS
cutter, pruning, guard-before-every-segment, quota stop and continuity rule -
on synthetic TS packets built below.

What does not: the real relay, ffmpeg and a camera-like source. That is
stack/door/tests/door-relay-lab.sh (root + docker; TC-1385..TC-1392).
Implements: TC-1377..TC-1384, TC-1393..TC-1398.
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


def test_tc1377_the_recorder_cannot_start_ahead_of_the_mount_or_outlive_it():
    u = unit("homehub-door-recorder.service")
    for line in ("RequiresMountsFor=/srv/scratch", "BindsTo=srv-scratch.mount",
                 "CapabilityBoundingSet=~CAP_SYS_RESOURCE", "RestartPreventExitStatus=75 76",
                 "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/scratch/homehub_scratch.py guard --writer door",
                 "WantedBy=multi-user.target srv-scratch.mount"):
        assert line in u.splitlines(), line
    assert "After=srv-scratch.mount" in u
    # The recorder's container has no restart policy of its own and mounts nothing.
    argv = rec.ffmpeg_argv("/run/x.env")
    assert "--restart" not in argv and "-v" not in argv and "--volume" not in argv
    assert "--rm" in argv and "--read-only" in argv and "ALL" in argv
    assert not any("rtsp://" in a for a in argv)  # the URL is in the env file, not argv


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
PROGRESS = [{"consumers": 1, "sent_bytes": 1000}, {"consumers": 1, "sent_bytes": 9000}]


class FakeConsumer:
    def poll(self):
        return None  # always "running": liveness alone must not be enough

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def run_gate(monkeypatch, tmp_path, capsys, probe_result=None, counters=None, **env):
    s = dr.RelaySettings(write_env(tmp_path, **env))
    monkeypatch.setattr(dr, "probe", lambda *a, **k: dict(probe_result or HEALTHY))
    monkeypatch.setattr(dr, "upstream_count", lambda _s: 1)
    monkeypatch.setattr(dr, "streams", lambda _s: {"producers": 1, "consumers": 2})
    seq = iter(PROGRESS if counters is None else counters)
    monkeypatch.setattr(dr, "consumer_bytes", lambda _s: next(seq))
    monkeypatch.setattr(dr, "image_ref", lambda: "example/go2rtc:1@sha256:0")
    monkeypatch.setattr(dr.subprocess, "Popen", lambda *a, **k: FakeConsumer())
    monkeypatch.setattr(dr.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(dr.time, "sleep", lambda _x: None)
    code = dr.gate(s, 1)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert lines[-1]["check"] == "gate-hub-half" and lines[-1]["pass"] is (code == 0)
    return code, {l["check"]: l for l in lines}


needs_fchmod = pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes (the gate's env files)")


@needs_fchmod
def test_tc1399_a_healthy_relay_passes_the_hub_half(monkeypatch, tmp_path, capsys):
    code, checks = run_gate(monkeypatch, tmp_path, capsys)
    assert code == 0 and checks["one-consumer-left"]["survivor_progressing"]
    assert checks["readiness+geometry"]["reasons"] == []


@needs_fchmod
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


@needs_fchmod
def test_tc1399_the_expected_stream_is_a_knob(monkeypatch, tmp_path, capsys):
    code, _ = run_gate(monkeypatch, tmp_path, capsys, probe_result=dict(HEALTHY, codec="hevc", width=960, height=960),
                       DOOR_RELAY_EXPECT_CODEC="hevc", DOOR_RELAY_EXPECT_SIZE="960x960")
    assert code == 0
    with pytest.raises(dr.Invalid, match="DOOR_RELAY_EXPECT_SIZE"):
        dr.RelaySettings(write_env(tmp_path, DOOR_RELAY_EXPECT_SIZE="big"))


@needs_fchmod
@pytest.mark.parametrize("counters", [
    [{"consumers": 1, "sent_bytes": 5000}, {"consumers": 1, "sent_bytes": 5000}],  # frozen
    [None, None],                                                                    # API silent
    [{"consumers": 1, "sent_bytes": 5000}, {"consumers": 0, "sent_bytes": 9000}],  # survivor gone
])
def test_tc1399_a_survivor_that_is_running_but_not_being_sent_video_fails(monkeypatch, tmp_path, capsys, counters):
    code, checks = run_gate(monkeypatch, tmp_path, capsys, counters=counters)
    assert code == 1 and checks["one-consumer-left"]["survivor_running"] is True
    assert checks["one-consumer-left"]["survivor_progressing"] is False


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
